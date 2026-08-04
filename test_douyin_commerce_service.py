# -*- coding: utf-8 -*-
"""抖音带货首版的离线边界与桌面页面回归测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QComboBox

from app_core import (
    douyin_commerce_service,
    douyin_commerce_session,
    douyin_music_service,
    douyin_publish_executor,
    publish_service,
    task_service,
)
from uploader.douyin_uploader.main import DouYinVideo
from ui.douyin_commerce_page import DouyinCommercePage


class DouyinCommercePayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.video = Path(self.tempdir.name) / "commerce.mp4"
        self.video.write_bytes(b"local-commerce-video")
        self.location = {
            "poiId": "poi-beihai-001",
            "name": "北海银滩景区",
            "address": "广西壮族自治区北海市银海区银滩大道中段",
            "distance": "6.0km",
        }
        # 仅覆盖历史兼容辅助函数；新带货任务不再保存或绑定门店。
        self.store = {
            "storeId": "store-001",
            "name": "银滩示例门店",
            "address": "广西壮族自治区北海市银海区银滩大道 225 号",
            "poiId": "poi-beihai-001",
            "source": "douyin-visible",
        }
        target = datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=1)
        self.payload = {
            "type": 3,
            "workflow": "douyin-commerce",
            "commerceMode": "local-group-buy",
            "runtimeMode": "preflight",
            "debugDryRun": True,
            "contentType": "video",
            "title": "北海团购视频测试",
            "description": "只验证抖音带货任务的本地安全边界。",
            "fileList": [str(self.video)],
            "accountList": ["oneclick_3_offline.json"],
            "locationKeyword": "北海银滩景区",
            "locationPoi": dict(self.location),
            "locationScope": "domestic",
            "contentDeclaration": "内容由AI生成",
            "enableTimer": True,
            "scheduleTime": target.strftime("%Y-%m-%d %H:%M"),
            "backgroundMode": False,
        }

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_requires_one_account_one_video_and_future_timer(self) -> None:
        checked = douyin_commerce_service.validate_douyin_commerce_payload(self.payload)
        self.assertEqual(checked["accountList"], ["oneclick_3_offline.json"])
        self.assertEqual(checked["fileList"], [str(self.video)])
        self.assertNotIn("commerceStore", checked)
        self.assertEqual(checked["locationScope"], "domestic")
        self.assertEqual(checked["contentDeclaration"], "内容由AI生成")
        self.assertTrue(checked["enableTimer"])
        self.assertEqual(checked["musicMode"], "favorite-first")
        self.assertEqual(
            douyin_publish_executor._scheduled_time(checked).strftime("%Y-%m-%d %H:%M"),
            self.payload["scheduleTime"],
        )

        multiple = dict(self.payload, accountList=["a.json", "b.json"])
        with self.assertRaisesRegex(douyin_commerce_service.DouyinCommerceError, "一个"):
            douyin_commerce_service.validate_douyin_commerce_payload(multiple)
        multiple = dict(self.payload, fileList=[str(self.video), str(self.video)])
        with self.assertRaisesRegex(douyin_commerce_service.DouyinCommerceError, "一条"):
            douyin_commerce_service.validate_douyin_commerce_payload(multiple)
        stale = dict(self.payload, scheduleTime="2020-01-01 09:00")
        with self.assertRaisesRegex(douyin_publish_executor.DouyinPublishError, "晚于"):
            douyin_publish_executor._scheduled_time(stale)

    def test_immediate_publish_is_allowed_without_a_schedule_time(self) -> None:
        immediate = dict(self.payload, enableTimer=False, scheduleTime="")
        checked = douyin_commerce_service.validate_douyin_commerce_payload(immediate)

        self.assertFalse(checked["enableTimer"])
        self.assertEqual(checked["scheduleTime"], "")
        self.assertIsNone(douyin_publish_executor._scheduled_time(checked))

        invalid = dict(self.payload, enableTimer=False)
        with self.assertRaisesRegex(
            douyin_commerce_service.DouyinCommerceError,
            "未开启抖音带货定时发布时",
        ):
            douyin_commerce_service.validate_douyin_commerce_payload(invalid)

    def test_legacy_store_field_is_removed_from_new_payload(self) -> None:
        payload = dict(self.payload)
        payload["commerceStore"] = {"storeId": "old-store", "poiId": "other-poi"}
        checked = douyin_commerce_service.validate_douyin_commerce_payload(payload)
        self.assertNotIn("commerceStore", checked)

    def test_requires_explicit_location_scope_and_content_declaration(self) -> None:
        with self.assertRaisesRegex(
            douyin_commerce_service.DouyinCommerceError, "定位范围"
        ):
            douyin_commerce_service.validate_douyin_commerce_payload(
                dict(self.payload, locationScope="")
            )
        with self.assertRaisesRegex(
            douyin_commerce_service.DouyinCommerceError, "作品内容声明"
        ):
            douyin_commerce_service.validate_douyin_commerce_payload(
                dict(self.payload, contentDeclaration="自动判断")
            )

    def test_store_discovery_requires_content_but_not_store_or_timer(self) -> None:
        discovery = dict(self.payload)
        discovery.pop("scheduleTime")
        discovery["enableTimer"] = False
        checked = douyin_commerce_service.validate_douyin_commerce_discovery_payload(
            discovery
        )
        self.assertNotIn("commerceStore", checked)
        self.assertFalse(checked["enableTimer"])
        self.assertEqual(checked["locationPoi"]["poiId"], "poi-beihai-001")

        missing_content = dict(discovery, description="")
        with self.assertRaisesRegex(
            douyin_commerce_service.DouyinCommerceError, "作品文案"
        ):
            douyin_commerce_service.validate_douyin_commerce_discovery_payload(
                missing_content
            )

    def test_manual_music_requires_user_readback_only_at_full_preflight(self) -> None:
        upload = dict(self.payload)
        upload["musicMode"] = "favorite-manual"
        upload.pop("scheduleTime")
        upload["enableTimer"] = False
        checked_upload = douyin_commerce_service.validate_douyin_commerce_upload_payload(
            upload
        )
        self.assertEqual(checked_upload["musicMode"], "favorite-manual")
        self.assertNotIn("selectedMusic", checked_upload)
        self.assertNotIn("locationPoi", checked_upload)

        with self.assertRaisesRegex(
            douyin_commerce_service.DouyinCommerceError,
            "用户从当前收藏列表选择",
        ):
            douyin_commerce_service.validate_douyin_commerce_payload(
                dict(self.payload, musicMode="favorite-manual")
            )

        checked = douyin_commerce_service.validate_douyin_commerce_payload(
            dict(
                self.payload,
                musicMode="favorite-manual",
                selectedMusic={
                    "musicId": "music-001",
                    "title": "出埃及记",
                    "creator": "石Yuchi",
                    "duration": "01:08",
                },
            )
        )
        self.assertEqual(checked["selectedMusic"]["musicId"], "music-001")

    def test_store_readback_requires_exact_stable_identity(self) -> None:
        self.assertEqual(
            douyin_publish_executor._commerce_readback(self.store, dict(self.store))["storeId"],
            "store-001",
        )
        with self.assertRaisesRegex(douyin_publish_executor.DouyinPublishError, "不一致"):
            douyin_publish_executor._commerce_readback(
                self.store, dict(self.store, name="相似门店")
            )

    def test_no_unique_visible_control_stops_without_guessing(self) -> None:
        class EmptyPage:
            async def evaluate(self, _script):
                return {"count": 0}

        with self.assertRaisesRegex(
            douyin_commerce_service.DouyinCommerceError, "带货模式控件组"
        ):
            asyncio.run(
                douyin_commerce_service.read_commerce_store_candidates(
                    EmptyPage(), self.location
                )
            )

    def test_duplicate_dom_targets_stop_before_store_click(self) -> None:
        class Node:
            async def is_visible(self) -> bool:
                return True

            async def evaluate(self, _script):
                return "store-001"

        class Locator:
            async def count(self) -> int:
                return 2

            def nth(self, _index):
                return Node()

        class Listbox:
            def locator(self, _selector):
                return Locator()

        store = {
            "storeId": "store-001",
            "name": self.location["name"],
            "address": self.location["address"],
            "poiId": self.location["poiId"],
        }
        raw_row = dict(store, selected="false")

        with patch.object(
            douyin_commerce_service,
            "_ensure_local_group_buy_mode",
            new_callable=AsyncMock,
            return_value=object(),
        ), patch.object(
            douyin_commerce_service,
            "_open_store_selector",
            new_callable=AsyncMock,
            return_value=Listbox(),
        ), patch.object(
            douyin_commerce_service,
            "_store_option_descriptors",
            new_callable=AsyncMock,
            return_value=[raw_row],
        ):
            with self.assertRaisesRegex(
                douyin_commerce_service.DouyinCommerceError, "不是唯一可点击项"
            ):
                asyncio.run(
                    douyin_commerce_service.apply_commerce_store_to_page(
                        object(), store, self.location
                    )
                )

    def test_visible_store_identity_requires_name_address_and_poi(self) -> None:
        candidate = douyin_commerce_service.normalize_commerce_store(
            {
                "name": self.location["name"],
                "address": self.location["address"],
                "poiId": self.location["poiId"],
                "commerceInfo": "32件商品 · 13件返佣",
            }
        )
        self.assertIsNotNone(candidate)
        self.assertTrue(candidate["storeId"].startswith("visible:"))
        self.assertEqual(candidate["commerceInfo"], "32件商品 · 13件返佣")
        self.assertIsNone(
            douyin_commerce_service.normalize_commerce_store(
                {"name": self.location["name"], "poiId": self.location["poiId"]}
            )
        )

    def test_duplicate_visible_candidates_stop_instead_of_deduplicating(self) -> None:
        raw = {
            "name": self.location["name"],
            "address": self.location["address"],
            "poiId": self.location["poiId"],
        }
        with self.assertRaisesRegex(
            douyin_commerce_service.DouyinCommerceError, "重复可见身份"
        ):
            douyin_commerce_service.normalize_commerce_store_candidates(
                [raw, dict(raw)], location_poi=self.location
            )

    def test_commerce_location_result_uses_visible_address_and_store_identity(self) -> None:
        row = {
            "name": "夜南香北京烤鸭(万泉城店)",
            "address": "广西壮族自治区北海市银海区银滩大道万泉城二区北侧",
            "commerceInfo": "15件商品 · 15件返佣",
        }
        candidate = douyin_commerce_service.normalize_commerce_location_store_candidate(row)
        self.assertIsNotNone(candidate)
        self.assertTrue(candidate["poiId"].startswith("visible-poi:"))
        self.assertEqual(candidate["address"], row["address"])
        self.assertEqual(candidate["commerceStore"]["address"], row["address"])
        self.assertEqual(candidate["commerceStore"]["commerceInfo"], "15件商品 · 15件返佣")
        with self.assertRaisesRegex(douyin_commerce_service.DouyinCommerceError, "重复可见身份"):
            douyin_commerce_service.normalize_commerce_location_store_candidates([row, dict(row)])

    def test_location_candidate_never_carries_a_store_binding(self) -> None:
        row = {
            "name": "夜南香北京烤鸭(万泉城店)",
            "address": "广西壮族自治区北海市银海区银滩大道万泉城二区北侧",
            "commerceInfo": "15件商品 · 15件返佣",
        }
        location = douyin_commerce_service.normalize_commerce_location_candidate(row)
        candidates = douyin_commerce_service.normalize_commerce_location_candidates([row])

        self.assertIsNotNone(location)
        self.assertEqual(location["address"], row["address"])
        self.assertEqual(candidates[0]["name"], row["name"])
        self.assertNotIn("commerceStore", candidates[0])
        self.assertNotIn("commerceStore", location)

    def test_commerce_location_search_only_reads_current_editor_candidates(self) -> None:
        class SearchInput:
            def __init__(self) -> None:
                self.scroll_into_view_if_needed = AsyncMock()
                self.click = AsyncMock()
                self.fill = AsyncMock()

        field = SearchInput()

        class Page:
            wait_for_timeout = AsyncMock()

        page = Page()
        row = {
            "name": "夜南香北京烤鸭(万泉城店)",
            "address": "广西壮族自治区北海市银海区银滩大道万泉城二区北侧",
            "commerceInfo": "15件商品 · 15件返佣",
        }
        with patch.object(
            douyin_commerce_service, "_ensure_position_tag", new_callable=AsyncMock
        ) as position, patch.object(
            douyin_commerce_service, "_ensure_local_group_buy_mode", new_callable=AsyncMock
        ) as mode, patch.object(
            douyin_commerce_service, "_open_commerce_search_input", new_callable=AsyncMock,
            return_value=field,
        ) as open_search, patch.object(
            douyin_commerce_service, "set_commerce_location_scope", new_callable=AsyncMock
        ) as set_scope, patch.object(
            douyin_commerce_service,
            "_visible_commerce_location_result_snapshot",
            new_callable=AsyncMock,
            return_value=(None, [], ""),
        ), patch.object(
            douyin_commerce_service,
            "_wait_for_fresh_commerce_location_results",
            new_callable=AsyncMock,
            return_value=(object(), [row]),
        ):
            result = asyncio.run(
                douyin_commerce_service.search_commerce_location_store_candidates(
                    page, "北海夜南香", scope="domestic"
                )
            )

        position.assert_awaited_once()
        mode.assert_awaited_once()
        open_search.assert_awaited_once_with(page, mode.return_value)
        set_scope.assert_awaited_once_with(page, "domestic")
        field.fill.assert_awaited_once_with("北海夜南香", timeout=8_000)
        self.assertEqual(result[0]["name"], row["name"])
        self.assertNotIn("commerceStore", result[0])

    def test_location_scope_anchors_leaf_labels_inside_current_search_panel(self) -> None:
        """范围标签不能提升到包含“本地国内”全文的父节点后再做文字相等判断。"""

        class Control:
            def __init__(self) -> None:
                self.click = AsyncMock()

        class Page:
            def __init__(self) -> None:
                self.control = Control()
                self.wait_for_timeout = AsyncMock()
                self.evaluate = AsyncMock(
                    side_effect=[
                        {"state": "ready", "local": 1, "domestic": 1, "panelDepth": 2},
                        {"state": "selected", "local": 1, "domestic": 1, "panelDepth": 2},
                        {"state": "selected", "local": 1, "domestic": 1, "panelDepth": 2},
                    ]
                )

            def locator(self, selector: str):
                self.selector = selector
                return self.control

        page = Page()
        result = asyncio.run(
            douyin_commerce_service.set_commerce_location_scope(page, "国内")
        )

        self.assertEqual(result, "国内")
        self.assertEqual(
            page.selector,
            '[data-oneclick-commerce-location-scope="active"]',
        )
        page.control.click.assert_awaited_once_with(timeout=5_000)
        self.assertEqual(
            [item.args[0] for item in page.wait_for_timeout.await_args_list],
            [250, 150],
        )
        browser_script = page.evaluate.await_args_list[0].args[0]
        self.assertIn('data-oneclick-commerce-search-input="active"', browser_script)
        self.assertIn("const labelLeaves", browser_script)
        self.assertIn("const lowestCommonAncestor", browser_script)
        self.assertIn("const scopeGroup = lowestCommonAncestor", browser_script)
        self.assertIn("const localTarget = interactive(panel.local, scopeGroup)", browser_script)
        self.assertIn("const localSelected = isMarkedSelected(panel.local, scopeGroup)", browser_script)
        self.assertIn("current && current !== boundary", browser_script)
        self.assertIn("scope-selection-not-exclusive", browser_script)
        self.assertNotIn(".map(interactive)", browser_script)

    def test_location_search_switches_page_scope_before_entering_keyword(self) -> None:
        """平台在输入关键词后立刻检索，因此范围切换必须先于 fill。"""

        events: list[str] = []

        class SearchInput:
            async def scroll_into_view_if_needed(self, **_kwargs) -> None:
                events.append("scroll")

            async def click(self, **_kwargs) -> None:
                events.append("click")

            async def fill(self, value: str, **_kwargs) -> None:
                events.append(f"fill:{value}")

        field = SearchInput()

        class Page:
            async def wait_for_timeout(self, milliseconds: int) -> None:
                events.append(f"wait:{milliseconds}")

        page = Page()
        row = {
            "name": "北海夜南香",
            "address": "广西壮族自治区北海市银海区银滩大道万泉城二区北侧",
        }

        async def mark_scope(_page, scope: str) -> str:
            events.append(f"scope:{scope}")
            return "国内"

        with patch.object(
            douyin_commerce_service, "_ensure_position_tag", new_callable=AsyncMock
        ), patch.object(
            douyin_commerce_service, "_ensure_local_group_buy_mode", new_callable=AsyncMock,
            return_value=object(),
        ) as mode, patch.object(
            douyin_commerce_service, "_open_commerce_search_input", new_callable=AsyncMock,
            return_value=field,
        ), patch.object(
            douyin_commerce_service, "set_commerce_location_scope", side_effect=mark_scope,
        ), patch.object(
            douyin_commerce_service,
            "_visible_commerce_location_result_snapshot",
            new_callable=AsyncMock,
            return_value=(None, [], ""),
        ), patch.object(
            douyin_commerce_service,
            "_wait_for_fresh_commerce_location_results",
            new_callable=AsyncMock,
            return_value=(object(), [row]),
        ):
            result = asyncio.run(
                douyin_commerce_service.search_commerce_location_store_candidates(
                    page, "北海夜南香", scope="domestic"
                )
            )

        self.assertEqual(mode.await_count, 1)
        self.assertEqual(result[0]["name"], "北海夜南香")
        self.assertEqual(
            events,
            ["scroll", "click", "scope:domestic", "wait:450", "fill:北海夜南香"],
        )

    def test_location_search_waits_past_stale_local_candidates(self) -> None:
        """首次搜索不能因旧本地推荐列表已存在而立即返回。"""

        local_rows = [
            {
                "name": "北海银滩景区",
                "address": "广西壮族自治区北海市银海区银滩大道中段",
            }
        ]
        domestic_rows = [
            {
                "name": "夜南香北京烤鸭(遂宁店)",
                "address": "四川省遂宁市船山区示例路 88 号",
            }
        ]

        self.assertFalse(
            douyin_commerce_service._location_rows_match_keyword(local_rows, "遂宁夜南香")
        )
        self.assertTrue(
            douyin_commerce_service._location_rows_match_keyword(domestic_rows, "遂宁夜南香")
        )

        class Page:
            wait_for_timeout = AsyncMock()

        stale_signature = douyin_commerce_service._location_result_signature(local_rows)
        fresh_signature = douyin_commerce_service._location_result_signature(domestic_rows)
        with patch.object(
            douyin_commerce_service,
            "_visible_commerce_location_result_snapshot",
            new_callable=AsyncMock,
            side_effect=[
                (object(), local_rows, stale_signature),
                (object(), local_rows, stale_signature),
                (object(), domestic_rows, fresh_signature),
            ],
        ):
            _listbox, rows = asyncio.run(
                douyin_commerce_service._wait_for_fresh_commerce_location_results(
                    Page(),
                    baseline_signature=stale_signature,
                    keyword="遂宁夜南香",
                )
            )
        self.assertEqual(rows, domestic_rows)

    def test_content_declaration_retries_only_after_known_cover_prompt_is_dismissed(self) -> None:
        """横封面提示可关闭后重试一次，并始终限定在声明弹层内。"""

        class Option:
            def __init__(self, side_effect=None) -> None:
                self.click = AsyncMock(side_effect=side_effect)

        class Page:
            def __init__(self) -> None:
                self.keyboard = object()
                self.wait_for_timeout = AsyncMock()

        class DeclarationDialog:
            def __init__(self) -> None:
                self.get_by_text = unittest.mock.Mock(return_value=object())
                self.get_by_role = unittest.mock.Mock(return_value=object())

        app = object.__new__(DouYinVideo)
        app.skip_thumbnail = True
        first_option = Option(
            RuntimeError(
                "<div class='coverImgContainer'>你的作品可能会在精选频道</div> "
                "subtree intercepts pointer events"
            )
        )
        retried_option = Option()
        confirm_button = Option()
        dialog = DeclarationDialog()
        with patch.object(
            app,
            "_dismiss_commerce_cover_promotion",
            new_callable=AsyncMock,
            side_effect=["absent", "dismissed"],
        ) as dismiss, patch.object(
            app, "_open_content_declaration_dialog", new_callable=AsyncMock,
            return_value=dialog,
        ) as open_dialog, patch.object(
            app,
            "_content_declaration_dialog_state",
            new_callable=AsyncMock,
            return_value={"count": 0, "texts": []},
        ), patch.object(
            app,
            "_visible_enabled_items",
            new_callable=AsyncMock,
            side_effect=[[first_option], [retried_option], [confirm_button]],
        ), patch.object(
            app,
            "_visible_exact_text",
            new_callable=AsyncMock,
            return_value={"无需添加自主声明"},
        ):
            actual = asyncio.run(app.set_content_declaration(Page(), "无需添加自主声明"))

        self.assertEqual(actual, "无需添加自主声明")
        self.assertEqual(app.content_declaration_verification, "无需添加自主声明")
        first_option.click.assert_awaited_once()
        retried_option.click.assert_awaited_once_with(timeout=5_000)
        confirm_button.click.assert_awaited_once_with(timeout=5_000)
        self.assertEqual(dismiss.await_count, 2)
        self.assertEqual(open_dialog.await_count, 2)
        dialog.get_by_role.assert_any_call(
            "radio", name="无需添加自主声明", exact=True
        )
        dialog.get_by_role.assert_any_call("button", name="确定", exact=True)
        dialog.get_by_text.assert_not_called()

    def test_reopens_declaration_from_current_selected_value(self) -> None:
        """第二次切换必须从“自主声明 + 当前选项”入口重新打开弹层。"""

        class Opener:
            def __init__(self) -> None:
                self.scroll_into_view_if_needed = AsyncMock()
                self.click = AsyncMock()

        class Dialog:
            is_visible = AsyncMock(return_value=True)

        class MarkedDialog:
            def __init__(self) -> None:
                self.first = Dialog()
                self.count = AsyncMock(return_value=1)

        class Page:
            def __init__(self) -> None:
                self.wait_for_timeout = AsyncMock()
                self.get_by_text = unittest.mock.Mock()
                self.locator = unittest.mock.Mock(return_value=MarkedDialog())

        app = object.__new__(DouYinVideo)
        page = Page()
        opener = Opener()
        current_label = "自主声明 内容为个人观点或见解"

        async def visible_items(locator):
            return [opener] if locator == current_label else []

        page.get_by_text.side_effect = lambda label, exact=True: label
        with patch.object(
            app,
            "_content_declaration_dialog_state",
            new_callable=AsyncMock,
            side_effect=[
                {"count": 0, "texts": []},
                {"count": 1, "texts": ["对作品内容添加声明"]},
            ],
        ), patch.object(
            app,
            "_visible_enabled_items",
            new_callable=AsyncMock,
            side_effect=visible_items,
        ):
            dialog = asyncio.run(app._open_content_declaration_dialog(page))

        self.assertIs(dialog, page.locator.return_value.first)
        page.get_by_text.assert_any_call(current_label, exact=True)
        opener.scroll_into_view_if_needed.assert_awaited_once_with(timeout=3_000)
        opener.click.assert_awaited_once_with(timeout=5_000)

    def test_reopens_declaration_from_split_semiselect_control(self) -> None:
        """新版拆分“自主声明”和当前值时，回退到唯一可见的声明控件。"""

        class Opener:
            def __init__(self) -> None:
                self.scroll_into_view_if_needed = AsyncMock()
                self.click = AsyncMock()

        class Dialog:
            is_visible = AsyncMock(return_value=True)

        class MarkedDialog:
            def __init__(self) -> None:
                self.first = Dialog()
                self.count = AsyncMock(return_value=1)

        class Page:
            def __init__(self) -> None:
                self.wait_for_timeout = AsyncMock()
                self.get_by_text = unittest.mock.Mock()
                self.evaluate = AsyncMock(
                    return_value={
                        "count": 1,
                        "texts": ["自主声明 内容为个人观点或见解"],
                    }
                )
                self.locator = unittest.mock.Mock()

        app = object.__new__(DouYinVideo)
        page = Page()
        opener = Opener()
        marked_opener = object()
        marked_dialog = MarkedDialog()
        page.get_by_text.side_effect = lambda label, exact=True: label
        page.locator.side_effect = lambda selector: (
            marked_opener
            if selector == '[data-oneclick-douyin-declaration-opener="active"]'
            else marked_dialog
        )

        async def visible_items(locator):
            return [opener] if locator is marked_opener else []

        with patch.object(
            app,
            "_content_declaration_dialog_state",
            new_callable=AsyncMock,
            side_effect=[
                {"count": 0, "texts": []},
                {"count": 1, "texts": ["对作品内容添加声明"]},
            ],
        ), patch.object(
            app,
            "_visible_enabled_items",
            new_callable=AsyncMock,
            side_effect=visible_items,
        ):
            dialog = asyncio.run(app._open_content_declaration_dialog(page))

        self.assertIs(dialog, marked_dialog.first)
        page.evaluate.assert_awaited_once()
        opener.scroll_into_view_if_needed.assert_awaited_once_with(timeout=3_000)
        opener.click.assert_awaited_once_with(timeout=5_000)

    def test_split_declaration_control_stops_when_fallback_is_ambiguous(self) -> None:
        """拆分控件若不能唯一定位，绝不能点击任意同名声明或移动端预览。"""

        class Page:
            def __init__(self) -> None:
                self.get_by_text = unittest.mock.Mock(
                    side_effect=lambda label, exact=True: label
                )
                self.evaluate = AsyncMock(
                    return_value={
                        "count": 2,
                        "texts": [
                            "自主声明 内容由AI生成",
                            "作者声明：内容由AI生成",
                        ],
                    }
                )

        app = object.__new__(DouYinVideo)
        page = Page()

        async def no_visible_items(_locator):
            return []

        with patch.object(
            app,
            "_content_declaration_dialog_state",
            new_callable=AsyncMock,
            return_value={"count": 0, "texts": []},
        ), patch.object(
            app,
            "_visible_enabled_items",
            new_callable=AsyncMock,
            side_effect=no_visible_items,
        ):
            with self.assertRaisesRegex(RuntimeError, "不是唯一可点击节点（实际 2 个）"):
                asyncio.run(app._open_content_declaration_dialog(page))

        page.evaluate.assert_awaited_once()

    def test_content_declaration_does_not_retry_when_page_has_no_known_cover_prompt(self) -> None:
        """未知遮挡保留原始失败，不能以 force-click 绕过。"""

        class Option:
            def __init__(self) -> None:
                self.click = AsyncMock(side_effect=RuntimeError("未知平台弹层拦截"))

        class Page:
            def __init__(self) -> None:
                self.wait_for_timeout = AsyncMock()

        class DeclarationDialog:
            def get_by_text(self, *_args, **_kwargs):
                return object()

            def get_by_role(self, *_args, **_kwargs):
                return object()

        app = object.__new__(DouYinVideo)
        app.skip_thumbnail = True
        option = Option()
        with patch.object(
            app,
            "_dismiss_commerce_cover_promotion",
            new_callable=AsyncMock,
            return_value="absent",
        ) as dismiss, patch.object(
            app,
            "_open_content_declaration_dialog",
            new_callable=AsyncMock,
            return_value=DeclarationDialog(),
        ), patch.object(
            app,
            "_visible_enabled_items",
            new_callable=AsyncMock,
            return_value=[option],
        ):
            with self.assertRaisesRegex(RuntimeError, "未知平台弹层拦截"):
                asyncio.run(app.set_content_declaration(Page(), "无需添加自主声明"))

        self.assertEqual(dismiss.await_count, 2)
        option.click.assert_awaited_once_with(timeout=5_000)

    def test_location_scope_reports_scoped_counts_when_panel_pair_is_ambiguous(self) -> None:
        class Page:
            def __init__(self) -> None:
                self.wait_for_timeout = AsyncMock()
                self.evaluate = AsyncMock(
                    return_value={
                        "state": "ambiguous",
                        "reason": "pair-not-in-search-panel",
                        "local": 2,
                        "domestic": 1,
                    }
                )

        with self.assertRaisesRegex(
            douyin_commerce_service.DouyinCommerceError,
            "当前地点面板内本地 2 个、国内 1 个；pair-not-in-search-panel",
        ):
            asyncio.run(douyin_commerce_service.set_commerce_location_scope(Page(), "国内"))

    def test_location_search_opens_dynamic_input_before_waiting(self) -> None:
        """新版页面必须先打开“输入地理位置”，不能在初始 DOM 猜 input。"""

        class Page:
            def __init__(self) -> None:
                self.wait_for_timeout = AsyncMock()

        page = Page()
        store_control = object()
        field = object()
        initial_absent = douyin_commerce_service.DouyinCommerceError(
            "抖音带货位置输入框未能唯一显示（实际 0 个），已安全停止"
        )
        with patch.object(
            douyin_commerce_service,
            "_visible_commerce_search_input",
            new_callable=AsyncMock,
            side_effect=[initial_absent, initial_absent, field],
        ), patch.object(
            douyin_commerce_service, "_open_exact_select", new_callable=AsyncMock
        ) as open_select:
            result = asyncio.run(
                douyin_commerce_service._open_commerce_search_input(
                    page, store_control
                )
            )

        self.assertIs(result, field)
        open_select.assert_awaited_once_with(page, store_control, "带货位置")
        page.wait_for_timeout.assert_awaited_once_with(200)

    def test_location_search_reuses_existing_dynamic_input_without_clicking(self) -> None:
        class Page:
            pass

        page = Page()
        field = object()
        with patch.object(
            douyin_commerce_service,
            "_visible_commerce_search_input",
            new_callable=AsyncMock,
            return_value=field,
        ), patch.object(
            douyin_commerce_service, "_open_exact_select", new_callable=AsyncMock
        ) as open_select:
            result = asyncio.run(
                douyin_commerce_service._open_commerce_search_input(page, object())
            )

        self.assertIs(result, field)
        open_select.assert_not_awaited()

    def test_store_dom_scripts_keep_newline_regex_escaped(self) -> None:
        """页面脚本中的 ``\\n`` 必须交给浏览器解析，不能被 Python 展开。"""

        class Page:
            def __init__(self) -> None:
                self.script = ""

            async def evaluate(self, script):
                self.script = script
                return {"count": 0}

        class Node:
            def __init__(self) -> None:
                self.script = ""

            async def is_visible(self) -> bool:
                return True

            async def evaluate(self, script):
                self.script = script
                return {}

        class OptionLocator:
            def __init__(self, node) -> None:
                self.node = node

            async def count(self) -> int:
                return 1

            def nth(self, _index):
                return self.node

        class Listbox:
            def __init__(self, node) -> None:
                self.node = node

            def locator(self, _selector):
                return OptionLocator(self.node)

        page = Page()
        self.assertIsNone(asyncio.run(douyin_commerce_service._visible_store_listbox(page)))
        node = Node()
        asyncio.run(douyin_commerce_service._store_option_descriptors(Listbox(node)))
        asyncio.run(
            douyin_commerce_service._location_option_targets(
                Listbox(node),
                {
                    "name": "北海银滩景区",
                    "address": "广西壮族自治区北海市银海区银滩大道中段",
                },
            )
        )

        for script in (page.script, node.script):
            self.assertIn(r"split(/\n+/", script)
            self.assertNotIn("split(/\n+/", script)
        self.assertIn("const normalize", page.script)

    def test_apply_location_only_reads_location_without_store_binding(self) -> None:
        class Page:
            def __init__(self) -> None:
                self.wait_for_timeout = AsyncMock()

        class Target:
            def __init__(self) -> None:
                self.scroll_into_view_if_needed = AsyncMock()
                self.click = AsyncMock()

        row = {
            "name": "夜南香北京烤鸭(万泉城店)",
            "address": "广西壮族自治区北海市银海区银滩大道万泉城二区北侧",
            "commerceInfo": "15件商品 · 15件返佣",
        }
        page = Page()
        target = Target()
        with patch.object(
            douyin_commerce_service,
            "_ensure_local_group_buy_mode",
            new_callable=AsyncMock,
            return_value=object(),
        ) as mode, patch.object(
            douyin_commerce_service,
            "_open_store_selector",
            new_callable=AsyncMock,
            return_value=object(),
        ) as open_selector, patch.object(
            douyin_commerce_service,
            "_store_option_descriptors",
            new_callable=AsyncMock,
            return_value=[row],
        ), patch.object(
            douyin_commerce_service,
            "_location_option_targets",
            new_callable=AsyncMock,
            return_value=[target],
        ), patch.object(
            douyin_commerce_service,
            "_anchor_controls",
            new_callable=AsyncMock,
            return_value=(None, None, "带货模式", row["name"]),
        ), patch.object(
            douyin_commerce_service,
            "apply_commerce_store_to_page",
            new_callable=AsyncMock,
        ) as bind_store:
            result = asyncio.run(
                douyin_commerce_service.apply_commerce_location_to_page(page, row)
            )

        mode.assert_awaited_once_with(page)
        open_selector.assert_awaited_once_with(page, mode.return_value)
        target.click.assert_awaited_once_with(timeout=8_000)
        bind_store.assert_not_awaited()
        self.assertEqual(result["location"]["name"], row["name"])
        self.assertNotIn("commerceStore", result)


class DouyinCommerceMusicRuleTests(unittest.TestCase):
    def test_only_visible_favorite_modes_are_accepted(self) -> None:
        self.assertEqual(
            douyin_music_service.validate_favorite_music_mode("favorite-first"),
            "favorite-first",
        )
        self.assertEqual(
            douyin_music_service.validate_favorite_music_mode("favorite-manual"),
            "favorite-manual",
        )
        with self.assertRaisesRegex(douyin_music_service.DouyinMusicError, "收藏"):
            douyin_music_service.validate_favorite_music_mode("recommend-first")

    def test_music_readback_requires_visible_identity_and_duration(self) -> None:
        result = douyin_music_service.normalize_music_readback(
            {
                "musicId": "music-001",
                "title": "出埃及记",
                "creator": "石Yuchi",
                "duration": "01:08",
            }
        )
        self.assertEqual(result["musicId"], "music-001")
        self.assertEqual(result["duration"], "01:08")
        self.assertIsNone(
            douyin_music_service.normalize_music_readback({"title": "只有标题"})
        )
        self.assertIsNone(
            douyin_music_service.normalize_music_readback(
                {"title": "选择音乐", "duration": "01:08"}
            )
        )

    def test_music_readback_rejects_transient_player_zero_duration(self) -> None:
        self.assertIsNone(
            douyin_music_service.normalize_music_readback(
                {
                    "title": "推荐",
                    "creator": "",
                    "duration": "00:00",
                }
            )
        )

    def test_music_rows_keep_visible_order_when_platform_id_is_absent(self) -> None:
        rows = douyin_music_service._normalize_favorite_music_rows(
            [
                {
                    "marker": "favorite-0",
                    "title": "Loveyourself (OriginalRemix)",
                    "creator": "w别丢下甜甜",
                    "duration": "05:36",
                    "musicId": "",
                },
                {
                    "marker": "favorite-1",
                    "title": "出埃及记",
                    "creator": "石Yuchi",
                    "duration": "01:08",
                    "musicId": "",
                },
            ]
        )
        self.assertEqual(rows[0]["title"], "Loveyourself (OriginalRemix)")
        self.assertTrue(rows[0]["musicId"].endswith("favorite-index:1"))
        self.assertTrue(rows[1]["musicId"].endswith("favorite-index:2"))

    def test_music_rows_reject_duplicate_platform_identity(self) -> None:
        item = {
            "marker": "favorite-0",
            "title": "出埃及记",
            "creator": "石Yuchi",
            "duration": "01:08",
            "musicId": "music-001",
        }
        with self.assertRaisesRegex(douyin_music_service.DouyinMusicError, "重复身份"):
            douyin_music_service._normalize_favorite_music_rows(
                [item, dict(item, marker="favorite-1")]
            )

    def test_commerce_editor_skips_cover_and_requires_favorite_music(self) -> None:
        class App:
            pass

        app = App()
        checked = {
            "musicMode": "favorite-first",
            "locationPoi": {"poiId": "poi-001"},
            "contentDeclaration": "内容由AI生成",
        }
        douyin_publish_executor._configure_commerce_editor(app, checked)
        self.assertTrue(app.skip_thumbnail)
        self.assertEqual(app.music_payload, {"mode": "favorite-first"})
        self.assertEqual(app.content_declaration, "内容由AI生成")
        self.assertFalse(hasattr(app, "commerce_payload"))

        discovery = App()
        douyin_publish_executor._configure_commerce_editor(
            discovery, checked, discovery=True
        )
        self.assertTrue(discovery.skip_thumbnail)
        self.assertEqual(discovery.content_declaration, "内容由AI生成")
        self.assertFalse(hasattr(discovery, "commerce_payload"))

    def test_manual_music_is_not_allowed_to_fall_back_to_legacy_reupload(self) -> None:
        class App:
            pass

        with self.assertRaisesRegex(
            douyin_publish_executor.DouyinPublishError,
            "分步编辑会话",
        ):
            douyin_publish_executor._configure_commerce_editor(
                App(),
                {
                    "musicMode": "favorite-manual",
                    "locationPoi": {"poiId": "poi-001"},
                    "contentDeclaration": "内容由AI生成",
                },
            )

    def test_commerce_only_dismisses_the_known_cover_promotion_overlay(self) -> None:
        self.assertTrue(
            DouYinVideo._is_commerce_cover_promotion_interception(
                "<div class='coverImgContainer'>你的作品可能会在精选频道</div> "
                "subtree intercepts pointer events"
            )
        )
        self.assertFalse(
            DouYinVideo._is_commerce_cover_promotion_interception(
                "抖音发布定位未找到唯一入口"
            )
        )


class DouyinCommerceMusicAsyncTests(unittest.IsolatedAsyncioTestCase):
    class _DelayedDialog:
        def __init__(self) -> None:
            self.calls = 0

        async def evaluate(self, _script):
            self.calls += 1
            if self.calls == 1:
                return []
            return [
                {
                    "marker": "favorite-0",
                    "title": "Loveyourself (OriginalRemix)",
                    "creator": "w别丢下甜甜",
                    "duration": "05:36",
                    "musicId": "music-001",
                }
            ]

    async def test_music_rows_wait_for_delayed_favorite_cards(self) -> None:
        dialog = self._DelayedDialog()
        with patch(
            "app_core.douyin_music_service.asyncio.sleep", new=AsyncMock()
        ) as sleep:
            rows = await douyin_music_service._favorite_music_rows(
                dialog, attempts=3, interval_seconds=0
            )
        self.assertEqual(dialog.calls, 2)
        self.assertEqual(rows[0]["title"], "Loveyourself (OriginalRemix)")
        sleep.assert_awaited_once_with(0.0)

    async def test_music_rows_stop_after_bounded_empty_wait(self) -> None:
        dialog = self._DelayedDialog()
        with patch(
            "app_core.douyin_music_service.asyncio.sleep", new=AsyncMock()
        ):
            with self.assertRaisesRegex(douyin_music_service.DouyinMusicError, "秒内"):
                await douyin_music_service._favorite_music_rows(
                    dialog, attempts=1, interval_seconds=0
                )

class DouyinCommerceUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.page = DouyinCommercePage()
        self.location_a = {
            "poiId": "poi-a",
            "name": "北海银滩景区",
            "address": "广西壮族自治区北海市银海区银滩大道中段",
            "distance": "6.0km",
        }
        self.location_b = {
            "poiId": "poi-b",
            "name": "北海老街",
            "address": "广西壮族自治区北海市海城区珠海路",
            "distance": "7.1km",
        }

    def tearDown(self) -> None:
        self.page.close()

    def test_location_scope_and_declaration_controls_replace_store_binding(self) -> None:
        self.assertEqual(self.page.location_scope_combo.currentData(), "domestic")
        self.assertEqual(self.page.location_scope_combo.currentText(), "国内")
        self.assertEqual(self.page.location_scope_combo.itemData(1), "local")
        self.assertEqual(self.page.location_scope_combo.itemData(2), "domestic")
        declarations = [
            self.page.declaration_combo.itemData(index)
            for index in range(1, self.page.declaration_combo.count())
        ]
        self.assertEqual(
            declarations,
            list(douyin_commerce_service.CONTENT_DECLARATION_OPTIONS),
        )
        self.assertIsNone(self.page.findChild(QComboBox, "douyinCommerceStore"))

    def test_location_scope_change_clears_declaration_and_shows_full_address(self) -> None:
        self.page.location_scope_combo.setCurrentIndex(1)
        self.page._show_locations([self.location_a, self.location_b])
        self.page.location_result_list.setCurrentRow(0)
        self.page.declaration_combo.setCurrentIndex(1)
        self.page._declaration_applied = True
        self.assertIn("广西壮族自治区北海市银海区银滩大道中段", self.page.location_card.text())
        self.page.location_scope_combo.setCurrentIndex(2)
        self.assertFalse(self.page._declaration_applied)
        self.assertEqual(self.page._selected_declaration(), "")
        self.assertIn("尚未选择", self.page.declaration_card.text())

    def test_commerce_payload_omits_cover_and_requires_user_selected_music(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            video = Path(root) / "commerce.mp4"
            video.write_bytes(b"video")
            account = {"type": 3, "status": 1, "filePath": "oneclick_3_demo.json"}
            media = {"storedPath": str(video), "filename": "commerce.mp4", "coverPath": "should-not-pass.jpg"}
            self.page.title_input.setText("抖音带货测试")
            self.page.description_input.setPlainText("仅验证本地带货载荷。")
            self.page._selected_music = {
                "musicId": "music-001",
                "title": "出埃及记",
                "creator": "石Yuchi",
                "duration": "01:08",
            }
            with patch.object(self.page, "_selected_account", return_value=account), patch.object(
                self.page, "_selected_video", return_value=media
            ), patch.object(self.page, "_selected_location", return_value=self.location_a
            ):
                self.page.location_scope_combo.setCurrentIndex(1)
                self.page.declaration_combo.setCurrentIndex(1)
                payload = self.page.collect_payload("preflight")
                upload = self.page.collect_upload_payload()
        self.assertEqual(payload["musicMode"], "favorite-manual")
        self.assertNotIn("coverPath", payload)
        self.assertEqual(payload["selectedMusic"]["musicId"], "music-001")
        self.assertEqual(payload["locationScope"], "local")
        self.assertEqual(payload["contentDeclaration"], "内容由AI生成")
        self.assertFalse(payload["enableTimer"])
        self.assertEqual(payload["scheduleTime"], "")
        self.assertNotIn("commerceStore", payload)
        self.assertEqual(upload["musicMode"], "favorite-manual")
        self.assertNotIn("coverPath", upload)
        self.assertIn("出埃及记", self.page._summary()["music"])

    def test_music_candidates_are_not_auto_selected(self) -> None:
        self.page._session_id = "session-demo"
        self.page._show_music_candidates(
            [
                {
                    "musicId": "music-001",
                    "title": "出埃及记",
                    "creator": "石Yuchi",
                    "duration": "01:08",
                },
                {
                    "musicId": "music-002",
                    "title": "Loveyourself (OriginalRemix)",
                    "creator": "w别丢下甜甜",
                    "duration": "05:36",
                },
            ]
        )
        self.assertIsNone(self.page._selected_music_candidate())
        self.assertFalse(self.page.use_music_button.isEnabled())
        self.page.music_list.setCurrentRow(1)
        self.assertEqual(
            self.page._selected_music_candidate()["musicId"], "music-002"
        )
        self.assertTrue(self.page.use_music_button.isEnabled())

    def test_music_completion_reenables_location_after_runner_cleanup(self) -> None:
        """音乐成功回调早于后台任务清理，清理后必须重新启用地点控件。"""

        self.page._session_id = "session-demo"
        self.page._selected_music = {
            "musicId": "music-001",
            "title": "出埃及记",
            "creator": "石Yuchi",
            "duration": "01:08",
        }
        self.page.location_scope_combo.setCurrentIndex(1)
        with patch.object(self.page.runner, "is_running", return_value=True):
            self.page._sync_view()
        self.assertFalse(self.page.location_keyword.isEnabled())
        self.assertFalse(self.page.location_search_button.isEnabled())

        with patch.object(self.page.runner, "is_running", return_value=False):
            self.page._reset_button(self.page.use_music_button, "使用所选音乐")

        self.assertTrue(self.page.location_keyword.isEnabled())
        self.assertTrue(self.page.location_search_button.isEnabled())
        self.assertEqual(self.page.status_badge.text(), "继续配置")

    def test_completed_choices_collapse_empty_lists_and_keep_safe_music_replacement(self) -> None:
        """已回读的选择不应留下大块空白，但允许用户重新读取收藏音乐。"""

        self.page._session_id = "session-demo"
        self.page._selected_music = {
            "musicId": "music-001",
            "title": "出埃及记",
            "creator": "石Yuchi",
            "duration": "01:08",
        }
        self.page._locations = [dict(self.location_a)]
        self.page.location_result_list.addItem("北海银滩景区")
        self.page._location_applied = True
        self.page._sync_view()

        self.assertTrue(self.page.music_list.isHidden())
        self.assertTrue(self.page.location_result_list.isHidden())
        self.assertTrue(self.page.load_music_button.isEnabled())
        self.assertEqual(self.page.load_music_button.text(), "更换收藏音乐")

    def test_progress_stepper_allows_return_without_skipping_required_steps(self) -> None:
        self.page._session_id = "session-demo"
        self.page.pages.setCurrentIndex(1)
        self.page._sync_view()

        self.assertTrue(self.page.step_labels[0].isEnabled())
        self.assertTrue(self.page.step_labels[1].isEnabled())
        self.assertFalse(self.page.step_labels[2].isEnabled())
        self.page.step_labels[0].click()
        self.assertEqual(self.page.pages.currentIndex(), 0)

    def test_selected_declaration_cannot_enter_review_before_platform_readback(self) -> None:
        self.page._session_id = "session-demo"
        self.page._selected_music = {
            "musicId": "music-001",
            "title": "出埃及记",
            "creator": "石Yuchi",
            "duration": "01:08",
        }
        self.page.location_scope_combo.setCurrentIndex(1)
        self.page._location_applied = True
        self.page._show_locations([self.location_a])
        self.page.location_result_list.setCurrentRow(0)
        self.page._location_applied = True
        self.page.declaration_combo.setCurrentIndex(1)
        self.assertFalse(self.page._can_review())
        self.assertIn("等待写入", self.page.declaration_card.text())
        self.page._declaration_applied_success("内容由AI生成")
        self.assertTrue(self.page._can_review())

    def test_timer_is_optional_and_enters_review_after_required_platform_readbacks(self) -> None:
        self.page._session_id = "session-demo"
        self.page._selected_music = {
            "musicId": "music-001",
            "title": "出埃及记",
            "creator": "石Yuchi",
            "duration": "01:08",
        }
        self.page._show_locations([self.location_a])
        self.page.location_result_list.setCurrentRow(0)
        self.page._location_applied = True
        self.page.declaration_combo.setCurrentIndex(1)
        self.page._declaration_applied = True
        self.page._sync_view()

        self.assertFalse(self.page.timer_enabled.isChecked())
        self.assertTrue(self.page.timer_enabled.isEnabled())
        self.assertFalse(self.page.schedule_date.isEnabled())
        self.assertTrue(self.page._can_review())
        self.assertTrue(self.page.to_review_button.isEnabled())

        self.page.timer_enabled.setChecked(True)
        self.assertTrue(self.page.schedule_date.isEnabled())
        self.assertTrue(self.page.schedule_time.isEnabled())
        self.assertTrue(self.page._can_review())

        self.page.timer_enabled.setChecked(False)
        self.assertFalse(self.page.schedule_date.isEnabled())
        self.assertTrue(self.page._can_review())

    def test_location_candidate_requires_declaration_after_location_readback(self) -> None:
        candidate = douyin_commerce_service.normalize_commerce_location_candidates(
            [
                {
                    "name": "夜南香北京烤鸭(万泉城店)",
                    "address": "广西壮族自治区北海市银海区银滩大道万泉城二区北侧",
                    "commerceInfo": "15件商品 · 15件返佣",
                }
            ]
        )[0]
        self.page._session_id = "session-demo"
        self.page._selected_music = {
            "musicId": "music-001",
            "title": "出埃及记",
            "creator": "石Yuchi",
            "duration": "01:08",
        }
        self.page.location_scope_combo.setCurrentIndex(1)
        self.page._show_locations([candidate])
        self.page.location_result_list.setCurrentRow(0)
        result = {
            "location": {
                key: candidate[key] for key in ("poiId", "name", "address", "distance")
            }
        }
        self.page._location_applied_success(result)

        self.assertTrue(self.page._location_applied)
        self.assertFalse(self.page._declaration_applied)
        self.assertIn("万泉城二区北侧", self.page.location_card.text())
        self.assertIn("请选择作品内容声明", self.page.declaration_status.text())
        self.assertTrue(self.page.to_declaration_button.isEnabled())
        self.assertFalse(self.page._can_review())

    def test_navigation_has_douyin_commerce_entry(self) -> None:
        from ui.main_window import MainWindow

        window = MainWindow()
        try:
            labels = [label for label, _page, _icon in window.page_definitions]
            self.assertEqual(labels[4], "抖音带货")
            window.set_current_page_by_key("commerce")
            self.assertIs(window.tabs.currentWidget(), window.douyin_commerce)
        finally:
            window.close()

    def test_declaration_error_stays_in_current_window(self) -> None:
        """声明回读失败只能提示，不能因弹窗参数错误退出客户端。"""

        with patch("ui.douyin_commerce_page.QMessageBox.warning") as warning:
            self.page._declaration_error("平台未返回可回读的声明")
        warning.assert_called_once_with(
            self.page, "作品内容声明", "平台未返回可回读的声明"
        )
        self.assertIn("平台未返回", self.page.declaration_status.text())

    def test_restore_saved_content_restores_local_fields_without_uploading(self) -> None:
        account = {
            "id": 31,
            "type": 3,
            "status": 1,
            "filePath": "oneclick_3_demo.json",
            "profileName": "测试抖音账号",
        }
        media = {
            "id": 42,
            "storedPath": "/tmp/demo-commerce.mp4",
            "filename": "demo-commerce.mp4",
        }
        self.page.account_combo.addItem("测试抖音账号", account)
        self.page.video_combo.addItem("demo-commerce.mp4", media)
        saved = {
            "payload": {
                "accountId": 31,
                "accountFile": "oneclick_3_demo.json",
                "mediaId": 42,
                "mediaPath": "/tmp/demo-commerce.mp4",
                "title": "已保存的带货标题",
                "description": "已保存的带货文案",
                "tags": ["北海", "团购"],
            },
            "updatedAt": "2026-08-04 18:00",
        }
        with patch(
            "ui.douyin_commerce_page.douyin_commerce_draft_service.load_content_draft",
            return_value=saved,
        ), patch("ui.douyin_commerce_page.QMessageBox.information") as info:
            self.page.restore_saved_content()

        self.assertEqual(self.page._selected_account()["id"], 31)
        self.assertEqual(self.page._selected_video()["id"], 42)
        self.assertEqual(self.page.title_input.text(), "已保存的带货标题")
        self.assertEqual(self.page.description_input.toPlainText(), "已保存的带货文案")
        self.assertEqual(self.page._tags(), ["北海", "团购"])
        self.assertFalse(self.page._session_id)
        self.assertTrue(self.page.upload_button.isEnabled())
        info.assert_called_once()


class DouyinCommerceTaskPresentationTests(unittest.TestCase):
    def test_task_label_and_commerce_summary_derive_from_existing_payload_json(self) -> None:
        payload_json = (
            '[{"contentType":"video","workflow":"douyin-commerce",'
            '"locationPoi":{"name":"北海银滩景区","address":"银滩大道"},'
            '"locationScope":"domestic",'
            '"contentDeclaration":"内容由AI生成",'
            '"scheduleTime":"2026-08-05 09:00"}]'
        )
        task = task_service._attach_content_type({"payloadJson": payload_json, "taskNo": "T08040900-ABCD"})
        self.assertEqual(task["workflowLabel"], "抖音带货")
        self.assertEqual(task["taskTypeLabel"], "视频 · 抖音带货")
        self.assertIn("地点：北海银滩景区（银滩大道）", task["commerceSummary"])
        self.assertIn("范围：国内", task["commerceSummary"])
        self.assertIn("声明：内容由AI生成", task["commerceSummary"])

    def test_task_summary_marks_immediate_publish_without_a_schedule(self) -> None:
        payload_json = (
            '[{"contentType":"video","workflow":"douyin-commerce",'
            '"locationPoi":{"name":"北海银滩景区","address":"银滩大道"},'
            '"locationScope":"domestic",'
            '"contentDeclaration":"内容由AI生成",'
            '"enableTimer":false,"scheduleTime":""}]'
        )
        task = task_service._attach_content_type({"payloadJson": payload_json, "taskNo": "T08040900-EFGH"})
        self.assertIn("发布方式：立即发表", task["commerceSummary"])


class DouyinCommerceSessionContractTests(unittest.TestCase):
    def test_immediate_preflight_does_not_touch_schedule_controls(self) -> None:
        class OpenPage:
            def is_closed(self) -> bool:
                return False

        class Uploader:
            def __init__(self) -> None:
                self.location_verification = "北海银滩景区"
                self.set_schedule_time_douyin = AsyncMock()
                self.verify_prepublish_form = AsyncMock(return_value={"title_confirmed": True})

        upload_payload = {
            "accountList": ["oneclick_3_demo.json"],
            "fileList": ["/tmp/demo.mp4"],
            "title": "抖音带货立即发表测试",
            "description": "仅验证预检不触碰定时控件。",
            "tags": [],
        }
        music = {
            "musicId": "music-001",
            "title": "出埃及记",
            "creator": "石Yuchi",
            "duration": "01:08",
        }
        location = {
            "poiId": "poi-001",
            "name": "北海银滩景区",
            "address": "广西壮族自治区北海市银海区银滩大道中段",
        }
        uploader = Uploader()
        manager = douyin_commerce_session.DouyinCommerceSessionManager()
        manager._session = douyin_commerce_session._CommerceEditorSession(
            session_id="session-demo",
            upload_payload=upload_payload,
            account_name="测试账号",
            browser=None,
            context=None,
            page=OpenPage(),
            playwright=None,
            uploader=uploader,
            stage="declaration_selected",
            selected_music=music,
            location=location,
            location_scope="domestic",
            selected_declaration="内容由AI生成",
        )
        payload = {
            **upload_payload,
            "selectedMusic": music,
            "locationPoi": location,
            "locationScope": "domestic",
            "contentDeclaration": "内容由AI生成",
            "enableTimer": False,
            "scheduleTime": "",
        }

        result = asyncio.run(manager._preflight("session-demo", payload))

        uploader.set_schedule_time_douyin.assert_not_awaited()
        uploader.verify_prepublish_form.assert_awaited_once()
        self.assertFalse(result["scheduled"])
        self.assertIsNone(result["scheduledAt"])
        self.assertEqual(manager._session.schedule_time, "")

    def test_immediate_submit_uses_platform_receipt_without_schedule_readback(self) -> None:
        class OpenPage:
            def is_closed(self) -> bool:
                return False

        class PublishButton:
            def __init__(self) -> None:
                self.click = AsyncMock()

        class Uploader:
            def __init__(self, publish_button) -> None:
                self.publish_button = publish_button
                self.set_schedule_time_douyin = AsyncMock()
                self.wait_publish_button_ready = AsyncMock(return_value=publish_button)
                self._wait_formal_publish_result = AsyncMock(return_value={"status": "published"})

        upload_payload = {
            "accountList": ["oneclick_3_demo.json"],
            "fileList": ["/tmp/demo.mp4"],
            "title": "抖音带货立即发表测试",
            "description": "仅验证立即发表提交边界。",
            "tags": [],
        }
        music = {
            "musicId": "music-001",
            "title": "出埃及记",
            "creator": "石Yuchi",
            "duration": "01:08",
        }
        location = {
            "poiId": "poi-001",
            "name": "北海银滩景区",
            "address": "广西壮族自治区北海市银海区银滩大道中段",
        }
        payload = {
            **upload_payload,
            "selectedMusic": music,
            "locationPoi": location,
            "locationScope": "domestic",
            "contentDeclaration": "内容由AI生成",
            "enableTimer": False,
            "scheduleTime": "",
        }
        publish_button = PublishButton()
        uploader = Uploader(publish_button)
        manager = douyin_commerce_session.DouyinCommerceSessionManager()
        manager._session = douyin_commerce_session._CommerceEditorSession(
            session_id="session-demo",
            upload_payload=upload_payload,
            account_name="测试账号",
            browser=None,
            context=None,
            page=OpenPage(),
            playwright=None,
            uploader=uploader,
            stage="preflighted",
            selected_music=music,
            location=location,
            location_scope="domestic",
            selected_declaration="内容由AI生成",
            preflight_fingerprint=manager._preflight_fingerprint(payload),
            schedule_time="",
        )
        with patch(
            "utils.base_social_media.reveal_page_window", new_callable=AsyncMock
        ) as reveal, patch.object(
            douyin_publish_executor,
            "_scheduled_submission_readback",
            new_callable=AsyncMock,
        ) as scheduled_readback:
            result = asyncio.run(manager._submit("session-demo", payload))

        reveal.assert_awaited_once()
        uploader.set_schedule_time_douyin.assert_not_awaited()
        publish_button.click.assert_awaited_once_with(timeout=10_000)
        scheduled_readback.assert_not_awaited()
        self.assertFalse(result["scheduled"])
        self.assertIsNone(result["scheduledAt"])
        self.assertEqual(result["platformReceipt"], {"status": "published"})
        self.assertIsNone(manager._session)

    def test_preflight_payload_must_match_same_uploaded_editor_session(self) -> None:
        manager = douyin_commerce_session.DouyinCommerceSessionManager()
        upload_payload = {
            "accountList": ["oneclick_3_demo.json"],
            "fileList": ["/tmp/demo.mp4"],
            "title": "抖音带货测试",
            "description": "仅验证会话匹配。",
            "tags": ["本地团购"],
        }
        music = {
            "musicId": "music-001",
            "title": "出埃及记",
            "creator": "石Yuchi",
            "duration": "01:08",
        }
        location = {
            "poiId": "poi-001",
            "name": "北海银滩景区",
            "address": "广西壮族自治区北海市银海区银滩大道中段",
        }
        session = douyin_commerce_session._CommerceEditorSession(
            session_id="session-demo",
            upload_payload=upload_payload,
            account_name="测试账号",
            browser=None,
            context=None,
            page=object(),
            playwright=None,
            uploader=None,
            selected_music=music,
            location=location,
            location_scope="domestic",
            selected_declaration="内容由AI生成",
        )
        payload = {
            **upload_payload,
            "selectedMusic": dict(music),
            "locationPoi": dict(location),
            "locationScope": "domestic",
            "contentDeclaration": "内容由AI生成",
        }
        manager._assert_payload_matches_session(session, payload)
        with self.assertRaisesRegex(
            douyin_commerce_session.DouyinCommerceSessionError,
            "音乐",
        ):
            manager._assert_payload_matches_session(
                session,
                dict(payload, selectedMusic=dict(music, musicId="music-002")),
            )
        with self.assertRaisesRegex(
            douyin_commerce_session.DouyinCommerceSessionError,
            "账号、视频",
        ):
            manager._assert_payload_matches_session(
                session,
                dict(payload, title="上传后被修改的标题"),
            )

    def test_store_candidate_read_keeps_verified_platform_dropdown_for_binding(self) -> None:
        class OpenPage:
            def is_closed(self) -> bool:
                return False

        location = {
            "poiId": "poi-001",
            "name": "北海银滩景区",
            "address": "广西壮族自治区北海市银海区银滩大道中段",
        }
        store = {
            "storeId": "store-001",
            "name": "银滩示例门店",
            "address": "广西壮族自治区北海市银海区银滩大道 225 号",
            "poiId": "poi-001",
        }
        manager = douyin_commerce_session.DouyinCommerceSessionManager()
        manager._session = douyin_commerce_session._CommerceEditorSession(
            session_id="session-demo",
            upload_payload={},
            account_name="测试账号",
            browser=None,
            context=None,
            page=OpenPage(),
            playwright=None,
            uploader=None,
            stage="location_selected",
            location=location,
        )
        with patch.object(
            douyin_commerce_service,
            "read_commerce_store_candidates",
            new_callable=AsyncMock,
            return_value=[store],
        ) as read, patch.object(
            douyin_commerce_service,
            "close_commerce_store_selector",
            new_callable=AsyncMock,
        ) as close:
            result = asyncio.run(manager._load_stores("session-demo"))

        read.assert_awaited_once_with(manager._session.page, location)
        close.assert_not_awaited()
        self.assertEqual(result, [store])
        self.assertEqual(manager._session.stage, "stores_loaded")

    def test_location_search_uses_current_uploaded_editor_session(self) -> None:
        class OpenPage:
            def is_closed(self) -> bool:
                return False

        manager = douyin_commerce_session.DouyinCommerceSessionManager()
        manager._session = douyin_commerce_session._CommerceEditorSession(
            session_id="session-demo",
            upload_payload={},
            account_name="测试账号",
            browser=None,
            context=None,
            page=OpenPage(),
            playwright=None,
            uploader=None,
            stage="music_selected",
            selected_music={
                "musicId": "music-001",
                "title": "测试音乐",
                "creator": "测试作者",
                "duration": "01:00",
            },
        )
        expected = douyin_commerce_service.normalize_commerce_location_store_candidates(
            [
                {
                    "name": "夜南香北京烤鸭(万泉城店)",
                    "address": "广西壮族自治区北海市银海区银滩大道万泉城二区北侧",
                    "commerceInfo": "15件商品 · 15件返佣",
                }
            ]
        )
        with patch.object(
            douyin_commerce_session.douyin_commerce_service,
            "search_commerce_location_store_candidates",
            new_callable=AsyncMock,
            return_value=expected,
        ) as search:
            result = asyncio.run(
                manager._search_locations("session-demo", "北海", "domestic")
            )

        search.assert_awaited_once_with(manager._session.page, "北海", scope="domestic")
        self.assertEqual(result, expected)
        self.assertEqual(manager._session.commerce_location_candidates, expected)
        self.assertIsNone(manager._session.location)
        self.assertEqual(manager._session.location_scope, "domestic")

    def test_location_selection_does_not_bind_store_without_explicit_store_step(self) -> None:
        class OpenPage:
            def is_closed(self) -> bool:
                return False

        class Uploader:
            location_verification = ""

        candidate = douyin_commerce_service.normalize_commerce_location_candidates(
            [
                {
                    "name": "夜南香北京烤鸭(万泉城店)",
                    "address": "广西壮族自治区北海市银海区银滩大道万泉城二区北侧",
                    "commerceInfo": "15件商品 · 15件返佣",
                }
            ]
        )[0]
        manager = douyin_commerce_session.DouyinCommerceSessionManager()
        manager._session = douyin_commerce_session._CommerceEditorSession(
            session_id="session-demo",
            upload_payload={},
            account_name="测试账号",
            browser=None,
            context=None,
            page=OpenPage(),
            playwright=None,
            uploader=Uploader(),
            stage="music_selected",
            selected_music={
                "musicId": "music-001",
                "title": "测试音乐",
                "creator": "测试作者",
                "duration": "01:00",
            },
            commerce_location_candidates=[candidate],
        )
        expected = {
            "location": {
                key: candidate[key] for key in ("poiId", "name", "address", "distance")
            }
        }
        with patch.object(
            douyin_commerce_session.douyin_commerce_service,
            "apply_commerce_location_to_page",
            new_callable=AsyncMock,
            return_value=expected,
        ) as apply:
            result = asyncio.run(manager._apply_location("session-demo", candidate))

        apply.assert_awaited_once_with(manager._session.page, candidate)
        self.assertEqual(result, expected)
        self.assertEqual(manager._session.stage, "location_selected")
        self.assertEqual(manager._session.location, expected["location"])
        self.assertEqual(manager._session.stores, [])
        self.assertIsNone(manager._session.selected_store)
        self.assertEqual(manager._session.uploader.location_verification, expected["location"]["name"])

    def test_store_bind_does_not_turn_success_into_close_menu_failure(self) -> None:
        class OpenPage:
            def is_closed(self) -> bool:
                return False

        location = {
            "poiId": "poi-001",
            "name": "北海银滩景区",
            "address": "广西壮族自治区北海市银海区银滩大道中段",
        }
        store = {
            "storeId": "store-001",
            "name": "银滩示例门店",
            "address": "广西壮族自治区北海市银海区银滩大道 225 号",
            "poiId": "poi-001",
        }
        manager = douyin_commerce_session.DouyinCommerceSessionManager()
        manager._session = douyin_commerce_session._CommerceEditorSession(
            session_id="session-demo",
            upload_payload={},
            account_name="测试账号",
            browser=None,
            context=None,
            page=OpenPage(),
            playwright=None,
            uploader=None,
            stage="stores_loaded",
            location=location,
            stores=[store],
        )
        with patch.object(
            douyin_commerce_service,
            "apply_commerce_store_to_page",
            new_callable=AsyncMock,
            return_value=store,
        ) as apply, patch.object(
            douyin_commerce_service,
            "close_commerce_store_selector",
            new_callable=AsyncMock,
        ) as close:
            result = asyncio.run(manager._select_store("session-demo", "store-001"))

        apply.assert_awaited_once_with(manager._session.page, store, location)
        close.assert_not_awaited()
        self.assertEqual(result, store)
        self.assertEqual(manager._session.stage, "store_selected")


class DouyinCommerceRoutingTests(unittest.TestCase):
    def test_preflight_routes_to_commerce_executor(self) -> None:
        payload = {
            "type": 3,
            "workflow": "douyin-commerce",
            "contentType": "video",
            "runtimeMode": "preflight",
        }
        with patch.object(
            publish_service.douyin_publish_executor,
            "run_douyin_commerce_preflight_sync",
            return_value={"ok": True, "message": "字段回读完成"},
        ) as execute, patch.object(
            publish_service.task_service, "mark_task_running"
        ), patch.object(
            publish_service.task_service, "record_task_event"
        ), patch.object(
            publish_service.task_service, "mark_platform_result"
        ) as mark:
            publish_service._run_preflight({"id": 401}, [payload])
        execute.assert_called_once_with(payload, task_id=401)
        self.assertTrue(mark.call_args.kwargs["ok"])


if __name__ == "__main__":
    unittest.main()
