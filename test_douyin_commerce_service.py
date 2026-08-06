# -*- coding: utf-8 -*-
"""抖音带货首版的离线边界与桌面页面回归测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, call, patch
from zoneinfo import ZoneInfo

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QApplication, QComboBox, QFrame, QLabel, QPushButton

from app_core import (
    douyin_commerce_service,
    douyin_commerce_session,
    douyin_music_service,
    douyin_publish_executor,
    media_service,
    publish_service,
    task_service,
)
from uploader.douyin_uploader.main import DouYinVideo
from ui.background_task import BackgroundTask
from ui.douyin_commerce_page import DouyinCommercePage
from utils import base_social_media


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


class MediaServiceVideoMetadataTests(unittest.TestCase):
    def test_local_probe_fills_duration_and_resolution_when_material_record_is_incomplete(self) -> None:
        """素材库未落库视频信息时，只能从本机视频探测，不能留给平台读取。"""

        with tempfile.TemporaryDirectory() as tempdir:
            video = Path(tempdir) / "团购短片.mp4"
            video.write_bytes(b"local-video-placeholder")
            with patch(
                "app_core.media_service._ffprobe_path", return_value=Path("/usr/bin/ffprobe")
            ), patch(
                "app_core.media_service.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["ffprobe"],
                    returncode=0,
                    stdout=(
                        '{"streams":[{"width":720,"height":1280}],'
                        '"format":{"duration":"14.08"}}'
                    ),
                    stderr="",
                ),
            ) as run:
                metadata = media_service.video_display_metadata(
                    {"storedPath": str(video), "filename": video.name}
                )

        self.assertEqual(metadata["durationText"], "00:14")
        self.assertEqual(metadata["resolution"], "720 × 1280")
        self.assertEqual(run.call_count, 1)


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

    def test_background_task_forwards_progress_before_success(self) -> None:
        events: list[dict[str, str]] = []
        task = BackgroundTask(
            lambda report: (
                report(
                    {
                        "phase": "uploading_video",
                        "label": "正在上传视频",
                        "state": "running",
                    }
                ),
                {"ok": True},
            )[1]
        )
        task.signals.progressed.connect(events.append)

        task.run()

        self.assertEqual(events[0]["phase"], "uploading_video")

    def test_upload_progress_uses_only_fixed_non_sensitive_phase_copy(self) -> None:
        self.page._set_commerce_progress(
            {
                "phase": "uploading_video",
                "label": "不应展示执行器返回的原始文字",
                "state": "running",
            }
        )

        self.assertFalse(self.page.operation_progress_frame.isHidden())
        self.assertEqual(self.page.operation_progress.value(), 3)
        self.assertEqual(self.page.operation_progress_label.text(), "正在上传视频")

        self.page._clear_commerce_progress()
        self.assertTrue(self.page.operation_progress_frame.isHidden())

    def test_login_required_stops_in_content_page_and_only_offers_account_navigation(self) -> None:
        requested: list[bool] = []
        self.page.request_account_management.connect(lambda: requested.append(True))
        self.page._session_id = "session-demo"
        self.page._selected_music = {"musicId": "music-demo"}
        self.page.pages.setCurrentIndex(1)

        self.page._handle_login_required()

        self.assertEqual(self.page.pages.currentIndex(), 0)
        self.assertEqual(self.page._session_id, "")
        self.assertIsNone(self.page._selected_music)
        self.assertFalse(self.page.login_required_frame.isHidden())
        self.page.login_required_button.click()
        self.app.processEvents()
        self.assertEqual(requested, [True])

    def test_location_scope_and_declaration_controls_replace_store_binding(self) -> None:
        self.assertEqual(self.page.location_scope_combo.currentData(), "domestic")
        self.assertEqual(self.page.location_scope_combo.currentText(), "国内")
        self.assertEqual(self.page.location_scope_combo.itemData(1), "local")
        self.assertEqual(self.page.location_scope_combo.itemData(2), "domestic")
        declarations = list(self.page.declaration_buttons)
        self.assertEqual(
            declarations,
            list(douyin_commerce_service.CONTENT_DECLARATION_OPTIONS),
        )
        self.assertTrue(
            self.page.declaration_buttons["无需添加自主声明"].isChecked()
        )
        self.assertIsNone(self.page.findChild(QComboBox, "douyinCommerceStore"))
        self.assertIsNone(
            self.page.findChild(QComboBox, "douyinCommerceContentDeclaration")
        )

    def test_location_scope_change_keeps_confirmed_declaration_and_shows_full_address(self) -> None:
        self.page.location_scope_combo.setCurrentIndex(1)
        self.page._show_locations([self.location_a, self.location_b])
        with patch.object(self.page, "_start_location_write"):
            self.page.location_result_list.setCurrentRow(0)
        self.page._set_selected_declaration("内容由AI生成")
        self.page._declaration_applied = True
        self.page._confirmed_declaration = "内容由AI生成"
        self.assertIn(
            "广西壮族自治区北海市银海区银滩大道中段",
            self.page.location_candidate_card.text(),
        )
        self.page.location_scope_combo.setCurrentIndex(2)
        self.assertTrue(self.page._declaration_applied)
        self.assertEqual(self.page._selected_declaration(), "内容由AI生成")

    def test_platform_stage_follows_verified_readbacks(self) -> None:
        """平台页只能由已回读的状态推进，不能按表单是否出现猜测。"""

        self.page._session_id = "session-demo"
        self.assertEqual(self.page._current_platform_stage(), "music")

        self.page._selected_music = {
            "musicId": "music-001",
            "title": "出埃及记",
            "creator": "石Yuchi",
            "duration": "01:08",
        }
        self.assertEqual(self.page._current_platform_stage(), "location")

        self.page._selected_location_data = dict(self.location_a)
        self.page._location_applied = True
        self.assertEqual(self.page._current_platform_stage(), "declaration")

        self.page._set_selected_declaration("内容由AI生成")
        self.page._declaration_applied = True
        self.assertEqual(self.page._current_platform_stage(), "schedule")

    def test_stage_error_hides_automation_detail_and_keeps_retry_action(self) -> None:
        """底层选择器错误只能进入本机诊断，界面必须给出可执行的下一步。"""

        diagnostic = "Locator.click: Timeout 5000ms exceeded"
        self.page._set_stage_error("declaration", diagnostic)

        text = self.page._stage_error_labels["declaration"].text()
        self.assertIn("失败原因：", text)
        self.assertIn("下一步：", text)
        self.assertNotIn("Locator.click", text)

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
                self.page._set_selected_declaration("内容由AI生成")
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

    def test_upload_payload_defaults_to_background_mode(self) -> None:
        """内容页启动上传时默认不前置浏览器窗口。"""

        with tempfile.TemporaryDirectory() as root:
            video = Path(root) / "commerce.mp4"
            video.write_bytes(b"video")
            account = {"type": 3, "status": 1, "filePath": "oneclick_3_demo.json"}
            media = {"storedPath": str(video), "filename": "commerce.mp4"}
            self.page.title_input.setText("抖音带货测试")
            self.page.description_input.setPlainText("仅验证后台会话载荷。")
            with patch.object(self.page, "_selected_account", return_value=account), patch.object(
                self.page, "_selected_video", return_value=media
            ):
                payload = self.page.collect_upload_payload()

        self.assertTrue(payload["backgroundMode"])

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
        self.assertEqual(self.page.music_combo.currentIndex(), 0)
        self.page.music_combo.setCurrentIndex(2)
        self.assertEqual(
            self.page._selected_music_candidate()["musicId"], "music-002"
        )
        self.assertIsNone(self.page._selected_music)

    def test_first_music_dropdown_click_reads_then_reopens_same_dropdown(self) -> None:
        """首次点击音乐下拉只读取，任务完成后应直接展开同一个选择器。"""

        self.page._session_id = "session-demo"
        with patch.object(
            self.page, "_start_immediate_write", return_value=True
        ) as start:
            self.page.music_combo.showPopup()
        self.assertEqual(start.call_args.args[0], "music_read")

        self.page._music_candidates = [
            {"musicId": "music-001", "title": "出埃及记"}
        ]
        self.page._immediate_write_kind = "music_read"
        self.page._open_music_picker_after_load = True
        with patch("ui.douyin_commerce_page.QTimer.singleShot") as reopen:
            self.page._finish_immediate_write("music_read")

        reopen.assert_called_once()
        self.assertFalse(self.page._open_music_picker_after_load)

    def test_immediate_music_write_uses_shared_task_and_locks_other_platform_controls(
        self,
    ) -> None:
        """音乐选中即写入时，其他平台控件必须等待同一会话回读完成。"""

        self.page._session_id = "session-demo"
        candidate = {"musicId": "music-new", "title": "新音乐"}
        with patch.object(self.page.runner, "is_running", return_value=False), patch.object(
            self.page.runner, "run", return_value=True
        ) as run:
            self.page._start_music_write(candidate)

        self.assertEqual(run.call_args.args[0], "douyin_commerce_immediate_write")
        self.assertFalse(self.page.location_search_button.isEnabled())
        self.assertFalse(
            self.page.declaration_buttons["无需添加自主声明"].isEnabled()
        )

    def test_failed_immediate_music_write_restores_last_confirmed_dropdown_value(
        self,
    ) -> None:
        """平台回读失败后不能把客户端暂选误当成已写入结果。"""

        old_music = {"musicId": "music-old", "title": "旧音乐"}
        new_music = {"musicId": "music-new", "title": "新音乐"}
        self.page._selected_music = dict(old_music)
        self.page._pending_music = dict(new_music)
        self.page._show_music_candidates([old_music, new_music])

        self.page._immediate_music_failed("平台回读不一致")

        self.assertEqual(self.page._selected_music, old_music)
        self.assertEqual(self.page.music_combo.currentData(), old_music)

    def test_location_click_immediately_starts_platform_write(self) -> None:
        """地点候选点击后直接进入同一编辑会话回读，不再要求二次确认。"""

        self.page._session_id = "session-demo"
        self.page._show_locations([self.location_a])
        with patch.object(self.page, "_start_location_write") as write:
            self.page.location_result_list.setCurrentRow(0)

        write.assert_called_once_with(self.location_a)

    def test_declaration_defaults_to_none_and_click_immediately_starts_write(
        self,
    ) -> None:
        """声明以单选项呈现，默认值和用户改选均不能停留在客户端暂态。"""

        self.page._session_id = "session-demo"
        default = "无需添加自主声明"
        self.page._sync_view()
        self.assertTrue(self.page.declaration_buttons[default].isChecked())
        with patch.object(self.page, "_start_declaration_write") as write:
            self.page.declaration_buttons["内容由AI生成"].click()

        write.assert_called_once_with("内容由AI生成")

    def test_schedule_controls_are_available_before_other_platform_readbacks(self) -> None:
        """定时只写本地任务载荷，不应被音乐、地点或声明的回读顺序锁住。"""

        self.page._session_id = "session-demo"
        self.page._sync_view()

        self.assertTrue(self.page.timer_enabled.isEnabled())
        self.page.timer_enabled.setChecked(True)
        self.assertTrue(self.page.schedule_date.isEnabled())
        self.assertTrue(self.page.schedule_time.isEnabled())

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
        self.page._immediate_write_kind = "music"
        with patch.object(self.page.runner, "is_running", return_value=False):
            self.page._sync_view()
        self.assertFalse(self.page.location_keyword.isEnabled())
        self.assertFalse(self.page.location_search_button.isEnabled())

        self.page._finish_immediate_write("music")

        self.assertTrue(self.page.location_keyword.isEnabled())
        self.assertTrue(self.page.location_search_button.isEnabled())
        self.assertEqual(self.page.status_badge.text(), "继续配置")

    def test_completed_choices_keep_fixed_controls_without_stale_candidate_list(self) -> None:
        """已回读的定位不再折叠其他设置，且不能保留旧候选列表。"""

        self.page._session_id = "session-demo"
        self.page._selected_music = {
            "musicId": "music-001",
            "title": "出埃及记",
            "creator": "石Yuchi",
            "duration": "01:08",
        }
        self.page._locations = [dict(self.location_a)]
        self.page.location_result_list.addItem("北海银滩景区")
        self.page._selected_location_data = dict(self.location_a)
        self.page._location_applied = True
        self.page._sync_view()

        self.assertFalse(self.page.music_panel.isHidden())
        self.assertFalse(self.page.location_panel.isHidden())
        self.assertFalse(self.page.declaration_panel.isHidden())
        self.assertFalse(self.page.music_combo.isHidden())

    def test_platform_workspace_keeps_all_configuration_sections_visible(self) -> None:
        """音乐/地点在左栏，声明/定时在右栏，不能再轮流占据整页。"""

        self.page._session_id = "session-demo"
        self.page.pages.setCurrentIndex(1)
        self.page._sync_view()
        self.assertFalse(self.page.music_panel.isHidden())
        self.assertFalse(self.page.location_panel.isHidden())
        self.assertFalse(self.page.declaration_panel.isHidden())
        self.assertFalse(self.page.schedule_panel.isHidden())
        self.assertFalse(self.page.platform_review_dock.isHidden())
        self.assertFalse(self.page.music_combo.isHidden())
        self.assertTrue(self.page.timer_enabled.isEnabled())

        self.page._selected_music = {
            "musicId": "music-001",
            "title": "出埃及记",
            "creator": "石Yuchi",
            "duration": "01:08",
        }
        self.page._sync_view()
        self.assertTrue(self.page.location_keyword.isEnabled())

        self.assertTrue(
            self.page.declaration_buttons["内容由AI生成"].isEnabled()
        )
        self.assertTrue(self.page.timer_enabled.isEnabled())

    def test_platform_workspace_uses_two_columns_and_one_review_action(self) -> None:
        """平台设置采用固定双栏，检查动作固定在底部。"""

        self.page._saved_content_available = True
        self.page._session_id = "session-demo"
        self.page.pages.setCurrentIndex(1)
        self.page._sync_view()

        self.assertIsNotNone(self.page.findChild(QFrame, "douyinCommercePlatformLeftColumn"))
        self.assertIsNotNone(self.page.findChild(QFrame, "douyinCommercePlatformRightColumn"))
        self.assertIsNotNone(self.page.findChild(QFrame, "douyinCommercePlatformReviewDock"))
        self.assertFalse(self.page.restore_content_button.isEnabled())

        self.page._selected_music = {
            "musicId": "music-001",
            "title": "出埃及记",
            "creator": "石Yuchi",
            "duration": "01:08",
        }
        self.page._sync_view()

        self.assertTrue(self.page.location_keyword.isEnabled())

    def test_platform_workspace_orders_music_and_declaration_on_left(self) -> None:
        """平台设置按“音乐、声明 / 定位、定时”组织，减少来回寻找。"""

        left_layout = self.page.platform_left_column.layout()
        right_layout = self.page.platform_right_column.layout()

        self.assertIs(left_layout.itemAt(0).widget(), self.page.music_stage)
        self.assertIs(left_layout.itemAt(1).widget(), self.page.declaration_stage)
        self.assertIs(right_layout.itemAt(0).widget(), self.page.location_stage)
        self.assertIs(right_layout.itemAt(1).widget(), self.page.schedule_stage)

    def test_location_hint_does_not_imply_music_must_be_selected_first(self) -> None:
        """四项平台设置可独立操作，定位文案不能误导为串行步骤。"""

        self.assertNotIn("完成音乐选择后", self.page.location_status.text())

    def test_platform_workspace_hides_redundant_readback_copy_when_idle(self) -> None:
        """成功后的页面只保留用户所选值，不重复解释“已由编辑页回读”。"""

        self.page._session_id = "session-demo"
        self.page._selected_music = {
            "musicId": "music-001",
            "title": "收藏音乐",
            "creator": "测试作者",
            "duration": "01:08",
        }
        self.page._selected_location_data = dict(self.location_a)
        self.page._location_applied = True
        self.page._set_selected_declaration("无需添加自主声明")
        self.page._declaration_applied = True
        self.page._confirmed_declaration = "无需添加自主声明"
        self.page._sync_view()

        visible_text = "\n".join(
            label.text()
            for label in self.page.findChildren(QLabel)
            if not label.isHidden()
        )
        self.assertNotIn("已由当前抖音编辑页回读确认", visible_text)
        self.assertNotIn("已确认一首收藏音乐", visible_text)
        all_label_text = "\n".join(
            label.text() for label in self.page.findChildren(QLabel)
        )
        self.assertNotIn("已由当前抖音编辑页回读确认", all_label_text)
        self.assertNotIn("已确认一首收藏音乐", all_label_text)
        self.assertTrue(self.page.music_status.isHidden())
        self.assertTrue(self.page.declaration_status.isHidden())
        self.assertTrue(self.page.platform_session_status.isHidden())

    def test_empty_platform_candidate_lists_do_not_reserve_large_space(self) -> None:
        self.assertEqual(self.page.music_combo.count(), 1)
        self.assertEqual(self.page.location_result_list.minimumHeight(), 0)
        self.assertTrue(self.page.location_result_list.isHidden())

    def test_location_selection_replaces_candidates_with_full_address_card(self) -> None:
        """选中候选即写入；回读后只保留完整地址，不出现第二次确认动作。"""

        self.page._session_id = "session-demo"
        self.page._selected_music = {
            "musicId": "music-001",
            "title": "出埃及记",
            "creator": "石Yuchi",
            "duration": "01:08",
        }
        self.page._show_locations([self.location_a, self.location_b])
        with patch.object(self.page, "_start_location_write"):
            self.page.location_result_list.setCurrentRow(0)

        self.assertIs(
            self.page.location_view_stack.currentWidget(), self.page.location_candidate_view
        )
        self.assertIn(self.location_a["address"], self.page.location_candidate_card.text())
        self.assertIsNone(
            self.page.findChild(QPushButton, "douyinCommerceApplyLocation")
        )

        self.page._location_applied_success({"location": self.location_a})

        self.assertIs(
            self.page.location_view_stack.currentWidget(), self.page.location_applied_view
        )
        self.assertIn(self.location_a["address"], self.page.location_applied_card.text())
        self.assertFalse(self.page.change_location_button.isHidden())

    def test_location_keyword_change_clears_confirmed_location_and_declaration(self) -> None:
        """范围或关键词变化只清地点，不应清除已经独立回读的声明。"""

        self.page._selected_location_data = dict(self.location_a)
        self.page._location_applied = True
        self.page._set_selected_declaration("内容由AI生成")
        self.page._declaration_applied = True
        self.page._confirmed_declaration = "内容由AI生成"

        self.page.location_keyword.setText("北海老街")
        self.page._location_keyword_edited()

        self.assertIsNone(self.page._selected_location_data)
        self.assertFalse(self.page._location_applied)
        self.assertTrue(self.page._declaration_applied)
        self.assertEqual(self.page._selected_declaration(), "内容由AI生成")
        self.assertIs(self.page.location_view_stack.currentWidget(), self.page.location_search_view)

    def test_music_change_keeps_independently_confirmed_location_and_declaration(self) -> None:
        """音乐改选不应把已回读的地点和声明退回为未设置。"""

        self.page._session_id = "session-demo"
        self.page._selected_music = {
            "musicId": "music-old",
            "title": "旧音乐",
            "creator": "测试",
            "duration": "00:30",
        }
        self.page._selected_location_data = dict(self.location_a)
        self.page._locations = [dict(self.location_a)]
        self.page._location_applied = True
        self.page._set_selected_declaration("内容由AI生成")
        self.page._declaration_applied = True
        self.page._confirmed_declaration = "内容由AI生成"

        self.page._music_selected(
            {
                "musicId": "music-new",
                "title": "新音乐",
                "creator": "测试",
                "duration": "01:08",
            }
        )

        self.assertEqual(self.page._selected_location_data, self.location_a)
        self.assertTrue(self.page._location_applied)
        self.assertTrue(self.page._declaration_applied)
        # 已确认地点本身会收起搜索输入；音乐改选不能让它退回候选态。
        self.assertTrue(self.page.change_location_button.isEnabled())

    def test_content_preparation_uses_a_persistent_primary_action_dock(self) -> None:
        self.page.pages.setCurrentIndex(0)
        self.page._sync_view()

        self.assertIs(self.page.upload_button, self.page.operation_dock_upload)
        self.assertIs(self.page.upload_button.parent(), self.page.operation_dock)
        self.assertFalse(self.page.operation_dock.isHidden())
        self.assertEqual(self.page.operation_dock_status.text(), "待补全")

    def test_content_page_keeps_its_top_workflow_chrome_compact(self) -> None:
        """标题和三步进度只用于定位当前步骤，不能挤占内容准备区。"""

        self.page.resize(1600, 900)
        self.page.show()
        self.app.processEvents()

        header = self.page.findChild(QFrame, "douyinCommerceReferenceHeader")
        progress = self.page.findChild(QFrame, "douyinCommerceProgress")
        subtitle = self.page.findChild(QLabel, "douyinCommerceReferenceSubtitle")

        self.assertIsNotNone(header)
        self.assertIsNotNone(progress)
        self.assertIsNotNone(subtitle)
        self.assertTrue(subtitle.isHidden())
        self.assertTrue(all(card.minimumHeight() <= 48 for card in self.page.step_cards))
        self.assertLessEqual(header.height() + progress.height(), 104)

    def test_content_workbench_places_restore_save_and_upload_in_separate_roles(self) -> None:
        """恢复在账号栏，保存归视频栏底部，底栏只保留上传主操作。"""

        self.page.pages.setCurrentIndex(0)
        self.page._sync_view()

        account_column = self.page.findChild(QFrame, "douyinCommerceContentAccountColumn")
        content_column = self.page.findChild(QFrame, "douyinCommerceContentBodyColumn")
        video_column = self.page.findChild(QFrame, "douyinCommerceContentVideoColumn")

        self.assertIsNotNone(account_column)
        self.assertIsNotNone(content_column)
        self.assertIsNotNone(video_column)
        self.assertTrue(account_column.isAncestorOf(self.page.restore_content_button))
        self.assertFalse(content_column.isAncestorOf(self.page.save_content_button))
        self.assertTrue(video_column.isAncestorOf(self.page.save_content_button))
        video_layout = video_column.layout()
        self.assertIsNotNone(video_layout)
        self.assertIs(
            video_layout.itemAt(video_layout.count() - 1).widget(),
            self.page.save_content_button,
        )
        self.assertEqual(self.page.video_replace_button.text(), "选择视频")
        self.assertIs(self.page.operation_dock_upload.parent(), self.page.operation_dock)
        self.assertFalse(self.page.operation_dock.isHidden())

    def test_content_cards_keep_only_primary_section_titles(self) -> None:
        """三栏卡片不得再展示重复的副标题，避免用户先读说明再找操作。"""

        headings = self.page.findChildren(
            QLabel, "douyinCommerceReferenceCardEyebrow"
        )

        self.assertEqual([heading.text() for heading in headings], ["账号", "内容", "视频"])
        self.assertIsNone(
            self.page.findChild(QLabel, "douyinCommerceReferenceCardTitle")
        )

    def test_account_selector_shows_avatar_subject_and_account_name(self) -> None:
        """选中账号后，下拉框和身份卡必须回读真实头像、主体与账号名。"""

        with tempfile.TemporaryDirectory() as tempdir:
            avatar_path = Path(tempdir) / "douyin-avatar.png"
            avatar_image = QImage(48, 48, QImage.Format.Format_ARGB32)
            avatar_image.fill(QColor("#0B8A85"))
            self.assertTrue(avatar_image.save(str(avatar_path)))
            account = {
                "id": 88,
                "type": 3,
                "status": 1,
                "platformName": "抖音",
                "profileName": "夜南香",
                "userName": "逆浪风",
                "avatarPath": str(avatar_path),
                "filePath": "oneclick_88.json",
            }
            with patch(
                "ui.douyin_commerce_page.account_service.list_accounts",
                return_value=[account],
            ), patch(
                "ui.douyin_commerce_page.media_service.list_media", return_value=[]
            ):
                self.page.refresh()

            self.assertEqual(self.page.account_combo.itemText(1), "逆浪风 · 夜南香")
            self.assertFalse(self.page.account_combo.itemIcon(1).isNull())
            self.assertFalse(self.page.account_combo.isHidden())
            self.page.account_combo.setCurrentIndex(1)
            self.page._sync_content_cards()

            avatar = self.page.findChild(QLabel, "douyinCommerceAccountAvatar")
            self.assertIsNotNone(avatar)
            self.assertIsNotNone(avatar.pixmap())
            self.assertFalse(avatar.pixmap().isNull())
            self.assertEqual(self.page.account_identity_name.text(), "逆浪风")
            self.assertEqual(self.page.account_card.text(), "主体：夜南香")

    def test_content_page_exposes_the_approved_visual_regions(self) -> None:
        """内容准备必须使用已确认的视觉骨架，而非旧命令栏的变体。"""

        self.page.pages.setCurrentIndex(0)
        self.page._sync_view()

        self.assertIsNotNone(
            self.page.findChild(QFrame, "douyinCommerceReferenceHeader")
        )
        self.assertIsNotNone(
            self.page.findChild(QFrame, "douyinCommerceStageSwitcher")
        )
        self.assertIsNotNone(
            self.page.findChild(QFrame, "douyinCommerceAccountIdentityCard")
        )
        self.assertIsNotNone(
            self.page.findChild(QFrame, "douyinCommerceVideoPreview")
        )
        self.assertIsNotNone(
            self.page.findChild(QFrame, "douyinCommerceVideoReplace")
        )
        self.assertIsNotNone(
            self.page.findChild(QFrame, "douyinCommerceReferenceFooter")
        )
        self.assertIsNone(
            self.page.findChild(QFrame, "douyinCommerceCommandBar")
        )

    def test_content_preparation_keeps_aligned_three_column_geometry(self) -> None:
        """防止内容准备页退化为高度不齐、底部操作被挤走的通用表单。"""

        self.page.resize(1600, 900)
        self.page.show()
        self.app.processEvents()

        cards = [
            self.page.findChild(QFrame, object_name)
            for object_name in (
                "douyinCommerceContentAccountColumn",
                "douyinCommerceContentBodyColumn",
                "douyinCommerceContentVideoColumn",
            )
        ]
        self.assertTrue(all(card is not None and card.height() >= 500 for card in cards))
        self.assertLessEqual(max(card.y() for card in cards) - min(card.y() for card in cards), 3)
        self.assertGreater(cards[1].width(), cards[0].width())
        self.assertTrue(self.page.operation_dock.isVisible())
        self.assertGreaterEqual(self.page.operation_dock.height(), 70)

    def test_video_replace_selects_only_a_local_material(self) -> None:
        """“更换视频”只切换本机素材选择，不能触发上传或平台会话。"""

        self.page.video_combo.clear()
        self.page.video_combo.addItem("请选择视频", None)
        self.page.video_combo.addItem("第一条.mp4", {"id": "video-a"})
        self.page.video_combo.addItem("第二条.mp4", {"id": "video-b"})

        select_video = getattr(self.page, "_select_local_video", None)
        self.assertTrue(callable(select_video))
        if not callable(select_video):
            return
        self.assertTrue(select_video(2))
        self.assertEqual(self.page.video_combo.currentIndex(), 2)
        self.assertEqual(self.page._session_id, "")
        self.assertFalse(select_video(9))
        self.assertEqual(self.page.video_combo.currentIndex(), 2)

    def test_video_picker_rows_show_cover_and_elide_long_names(self) -> None:
        """视频选择菜单须以封面识别素材，长文件名不能撑宽菜单。"""

        with tempfile.TemporaryDirectory() as tempdir:
            cover_path = Path(tempdir) / "long-name-cover.png"
            cover = QImage(72, 108, QImage.Format.Format_ARGB32)
            cover.fill(QColor("#326180"))
            self.assertTrue(cover.save(str(cover_path)))
            long_name = "知言_随便_最终视频_v02_正式发布候选_北海夜南香团购探店_1920x1080_20260720.mp4"
            self.page.video_combo.clear()
            self.page.video_combo.addItem("请选择一个视频素材", None)
            self.page.video_combo.addItem(
                long_name,
                {
                    "id": 301,
                    "filename": long_name,
                    "storedPath": str(Path(tempdir) / "source.mp4"),
                    "coverPath": str(cover_path),
                },
            )

            build_picker = getattr(self.page, "_build_video_picker_menu", None)
            self.assertTrue(callable(build_picker))
            if not callable(build_picker):
                return
            menu = build_picker()
            rows = menu.findChildren(QPushButton, "douyinCommerceVideoPickerItem")

            self.assertEqual(len(rows), 1)
            self.assertFalse(rows[0].icon().isNull())
            self.assertLessEqual(len(rows[0].text()), 32)
            self.assertTrue(rows[0].text().endswith("…"))
            self.assertEqual(rows[0].toolTip(), long_name)

    def test_video_picker_menu_is_bounded_by_the_client_viewport(self) -> None:
        """长视频列表的选择菜单不得越过当前客户端的可用宽高。"""

        self.page.resize(460, 360)
        self.page.video_combo.clear()
        self.page.video_combo.addItem("请选择一个视频素材", None)
        for index in range(12):
            self.page.video_combo.addItem(
                f"北海夜南香团购视频_{index:02d}_超长文件名称.mp4",
                {
                    "id": 400 + index,
                    "filename": f"北海夜南香团购视频_{index:02d}_超长文件名称.mp4",
                    "storedPath": f"/tmp/video-{index}.mp4",
                },
            )

        build_picker = getattr(self.page, "_build_video_picker_menu", None)
        self.assertTrue(callable(build_picker))
        if not callable(build_picker):
            return
        menu = build_picker()

        self.assertLessEqual(menu.width(), self.page.width() - 32)
        self.assertLessEqual(menu.height(), self.page.height() - 32)

    def test_save_restore_never_creates_platform_session(self) -> None:
        """本机保存与恢复不得启动后台执行器或创建平台会话。"""

        saved = {"payload": {}, "updatedAt": "2026-08-05 12:00"}
        with patch(
            "ui.douyin_commerce_page.douyin_commerce_draft_service.save_content_draft",
            return_value=saved,
        ), patch(
            "ui.douyin_commerce_page.douyin_commerce_draft_service.load_content_draft",
            return_value=None,
        ), patch.object(
            self.page, "_remember_current_tags", return_value=[]
        ), patch("ui.douyin_commerce_page.QMessageBox.information"), patch.object(
            self.page.runner, "run"
        ) as run:
            self.page.save_content()
            self.page.restore_saved_content()

        run.assert_not_called()
        self.assertEqual(self.page._session_id, "")

    def test_video_card_shows_compact_material_metadata(self) -> None:
        """素材卡只保留文件名、时长和尺寸，避免把素材库内部状态暴露给用户。"""

        self.page.video_combo.addItem(
            "团购短片.mp4",
            {
                "storedPath": "/tmp/demo.mp4",
                "filename": "团购短片.mp4",
                "durationText": "00:32",
                "resolution": "1080 × 1920",
            },
        )
        self.page.video_combo.setCurrentIndex(self.page.video_combo.count() - 1)
        self.page._sync_content_cards()

        self.assertEqual(
            self.page.video_card.text(), "团购短片.mp4\n00:32 · 1080 × 1920"
        )

    def test_video_card_renders_the_existing_material_cover(self) -> None:
        """已选视频必须显示素材库已有的封面，而不是另建发布封面字段。"""

        with tempfile.TemporaryDirectory() as tempdir:
            cover = Path(tempdir) / "material-cover.png"
            image = QImage(72, 108, QImage.Format.Format_ARGB32)
            image.fill(QColor("#1D4E6D"))
            self.assertTrue(image.save(str(cover)))
            self.page.video_combo.addItem(
                "团购短片.mp4",
                {
                    "id": 99,
                    "storedPath": str(Path(tempdir) / "团购短片.mp4"),
                    "filename": "团购短片.mp4",
                    "coverPath": str(cover),
                    "durationText": "00:32",
                    "resolution": "1080 × 1920",
                },
            )
            self.page.video_combo.setCurrentIndex(self.page.video_combo.count() - 1)
            self.page._sync_content_cards()

        self.assertFalse(self.page.video_thumbnail.pixmap().isNull())
        self.assertEqual(self.page.video_thumbnail.text(), "")
        self.assertEqual(
            self.page.video_card.text(), "团购短片.mp4\n00:32 · 1080 × 1920"
        )

    def test_content_page_uses_three_columns_and_tag_chips(self) -> None:
        """账号、内容和视频三栏均保留独立控件，标签可显式添加和移除。"""

        self.page.tags_input.setText("北海 本地团购")
        self.page.add_tags_from_input()

        self.assertEqual(self.page._tags(), ["北海", "本地团购"])
        self.assertEqual(self.page.selected_tags_layout.count(), 3)
        self.assertTrue(self.page.findChild(QFrame, "douyinCommerceThreeColumn"))
        self.page.remove_tag("北海")
        self.assertEqual(self.page._tags(), ["本地团购"])

    def test_content_page_keeps_change_feedback_in_primary_action_only(self) -> None:
        """内容变更由底部主操作表达，不额外堆叠一条长提示。"""

        self.assertTrue(self.page.content_notice.isHidden())
        self.page._session_id = "session-demo"
        self.page._content_changed()
        self.assertTrue(self.page.content_notice.isHidden())

    def test_unchanged_content_returns_to_platform_settings_without_runner_work(self) -> None:
        """回到内容页但未改任何字段时，只切换页面，不启动后台任务。"""

        payload = {
            "accountList": ["oneclick_3_demo.json"],
            "fileList": ["/tmp/demo.mp4"],
            "title": "原标题",
            "description": "原文案",
            "tags": ["北海"],
        }
        self.page._session_id = "session-demo"
        self.page._uploaded_editor_payload = dict(payload)
        continue_after_content = getattr(self.page, "continue_after_content", None)
        self.assertTrue(callable(continue_after_content))
        with patch.object(self.page, "collect_upload_payload", return_value=dict(payload)), patch.object(
            self.page.runner, "run"
        ) as run:
            continue_after_content()

        run.assert_not_called()
        self.assertEqual(self.page.pages.currentIndex(), 1)

    def test_text_only_change_uses_content_sync_not_video_upload(self) -> None:
        """标题、文案或标签变化只能同步同一编辑页，不能重传视频。"""

        payload = {
            "accountList": ["oneclick_3_demo.json"],
            "fileList": ["/tmp/demo.mp4"],
            "title": "原标题",
            "description": "原文案",
            "tags": ["北海"],
        }
        changed = dict(payload, title="仅修改标题")
        self.page._session_id = "session-demo"
        self.page._uploaded_editor_payload = dict(payload)
        continue_after_content = getattr(self.page, "continue_after_content", None)
        self.assertTrue(callable(continue_after_content))
        with patch.object(self.page, "collect_upload_payload", return_value=changed), patch.object(
            self.page.runner, "run", return_value=True
        ) as run:
            continue_after_content()

        self.assertEqual(run.call_args.args[0], "douyin_commerce_sync_content")

    def test_account_or_video_change_requires_full_upload(self) -> None:
        """账号或视频变化必须走完整上传，不能复用旧会话。"""

        payload = {
            "accountList": ["oneclick_3_demo.json"],
            "fileList": ["/tmp/demo.mp4"],
            "title": "原标题",
            "description": "原文案",
            "tags": ["北海"],
        }
        self.page._session_id = "session-demo"
        self.page._uploaded_editor_payload = dict(payload)
        content_change_kind = getattr(self.page, "_content_change_kind", None)
        self.assertTrue(callable(content_change_kind))
        with patch.object(
            self.page,
            "collect_upload_payload",
            return_value=dict(payload, fileList=["/tmp/other.mp4"]),
        ):
            self.assertEqual(content_change_kind(), "reupload")

    def test_reupload_failure_discards_the_closed_old_editor_session(self) -> None:
        """重新上传失败后，不能继续把已关闭的旧会话当作可用。"""

        self.page._session_id = "old-session"
        self.page._uploaded_editor_payload = {
            "accountList": ["oneclick_3_old.json"],
            "fileList": ["/tmp/old.mp4"],
            "title": "旧标题",
            "description": "旧文案",
            "tags": [],
        }
        self.page._pending_upload_payload = {
            "accountList": ["oneclick_3_new.json"],
            "fileList": ["/tmp/new.mp4"],
        }

        with patch.object(self.page, "_platform_action_error"):
            self.page._upload_failed("新视频上传失败")

        self.assertEqual(self.page._session_id, "")
        self.assertIsNone(self.page._uploaded_editor_payload)
        self.assertIsNone(self.page._pending_upload_payload)

    def test_location_selection_collapses_candidates_until_user_changes_it(self) -> None:
        """选中地点后立即进入写入状态，不让候选下拉持续占据页面。"""

        self.page._session_id = "session-demo"
        self.page._selected_music = {
            "musicId": "music-001",
            "title": "出埃及记",
            "creator": "石Yuchi",
            "duration": "01:08",
        }
        self.page._show_locations([self.location_a, self.location_b])
        with patch.object(self.page, "_start_location_write"):
            self.page.location_result_list.setCurrentRow(0)

        self.assertIs(
            self.page.location_view_stack.currentWidget(), self.page.location_candidate_view
        )
        self.assertIn("正在写入平台", self.page.location_candidate_card.text())
        self.page.change_location_selection()
        self.assertIs(
            self.page.location_view_stack.currentWidget(), self.page.location_search_view
        )

    def test_progress_indicator_is_noninteractive_and_back_navigation_does_not_skip(self) -> None:
        """步骤条只表达状态；真正的前进/返回由当前页操作按钮控制。"""

        self.page._session_id = "session-demo"
        self.page.pages.setCurrentIndex(1)
        self.page._sync_view()

        self.assertEqual(self.page.step_labels[0].property("stepState"), "complete")
        self.assertEqual(self.page.step_labels[1].property("stepState"), "active")
        self.assertEqual(self.page.step_labels[2].property("stepState"), "pending")
        self.assertFalse(hasattr(self.page.step_labels[0], "click"))
        self.page._go_to_step(0)
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
        self.page._selected_location_data = dict(self.location_a)
        self.page._location_applied = True
        self.page._set_selected_declaration("内容由AI生成")
        self.assertFalse(self.page._can_review())
        self.assertIn("尚未", self.page.declaration_card.text())
        self.page._pending_declaration = "内容由AI生成"
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
        self.page._selected_location_data = dict(self.location_a)
        self.page._location_applied = True
        self.page._set_selected_declaration("内容由AI生成")
        self.page._declaration_applied = True
        self.page._confirmed_declaration = "内容由AI生成"
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

    def test_platform_review_dock_places_return_before_check_action(self) -> None:
        """平台设置底部应先给返回内容，再给继续检查。"""

        layout = self.page.platform_review_dock.layout()

        self.assertIs(layout.itemAt(0).widget(), self.page.platform_review_status)
        self.assertIs(layout.itemAt(1).widget(), self.page.platform_back_button)
        self.assertIs(layout.itemAt(2).widget(), self.page.to_review_button)

    def test_continue_to_review_syncs_schedule_before_navigation(self) -> None:
        """检查页前必须先核对编辑页定时；仅在回读成功后才进入下一步。"""

        self.page._session_id = "session-demo"
        self.page._selected_music = {
            "musicId": "music-001",
            "title": "出埃及记",
            "creator": "石Yuchi",
            "duration": "01:08",
        }
        self.page._selected_location_data = dict(self.location_a)
        self.page._location_applied = True
        self.page._set_selected_declaration("内容由AI生成")
        self.page._declaration_applied = True
        self.page._confirmed_declaration = "内容由AI生成"
        self.page.timer_enabled.setChecked(True)
        expected_schedule = self.page._schedule_text()
        payload = {"enableTimer": True, "scheduleTime": expected_schedule}

        def start_schedule_write(kind, _work, _on_success, _on_error):
            self.page._immediate_write_kind = kind
            self.page._sync_view()
            return True

        with patch.object(self.page, "collect_payload", return_value=payload), patch.object(
            self.page, "_start_immediate_write", side_effect=start_schedule_write
        ) as start, patch.object(self.page, "_go_to_step") as go:
            self.page.continue_to_review()

            self.assertEqual(start.call_args.args[0], "schedule")
            self.assertEqual(self.page.platform_review_status.text(), "正在检查发布时间…")
            self.assertEqual(self.page.to_review_button.text(), "正在检查…")
            sync_succeeded = start.call_args.args[2]
            go.assert_not_called()
            sync_succeeded({"status": "updated", "scheduledAt": expected_schedule})

        go.assert_called_once_with(2)

    def test_review_summary_shows_full_address_and_content_preview(self) -> None:
        """检查页只能汇总已回读字段，并保留完整地点与文案摘要供人工核对。"""

        self.page.title_input.setText("北海团购视频")
        self.page.description_input.setPlainText("这是用于人工检查的完整作品文案。")
        self.page._session_id = "session-demo"
        self.page._selected_music = {
            "musicId": "music-001",
            "title": "出埃及记",
            "creator": "石Yuchi",
            "duration": "01:08",
        }
        self.page._selected_location_data = dict(self.location_a)
        self.page._location_applied = True
        self.page._set_selected_declaration("内容由AI生成")
        self.page._declaration_applied = True
        self.page._confirmed_declaration = "内容由AI生成"

        self.page._go_to_step(2)

        self.assertIn(self.location_a["address"], self.page.summary_values["location"].text())
        self.assertEqual(self.page.summary_values["declaration"].text(), "内容由AI生成")
        self.assertIn("完整作品文案", self.page.summary_values["description"].text())

    def test_abandon_session_keeps_saved_content_recoverable(self) -> None:
        """放弃临时会话只清平台内存状态，不能删除或锁死本机保存内容。"""

        self.page._saved_content_available = True
        self.page._session_id = "session-demo"
        self.page.restore_content_button.setEnabled(False)
        with patch(
            "ui.douyin_commerce_page.douyin_commerce_session.commerce_session_manager.close"
        ) as close, patch(
            "ui.douyin_commerce_page.douyin_commerce_draft_service.load_content_draft",
            return_value={"payload": {}, "updatedAt": "2026-08-05 12:00"},
        ):
            self.page._abandon_session(silent=True)

        close.assert_called_once_with("session-demo")
        self.assertEqual(self.page._session_id, "")
        self.assertTrue(self.page._saved_content_available)
        self.assertTrue(self.page.restore_content_button.isEnabled())

    def test_location_candidate_keeps_independent_declaration_controls_after_location_readback(self) -> None:
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
        with patch.object(self.page.runner, "run", return_value=True):
            self.page.location_result_list.setCurrentRow(0)
        result = {
            "location": {
                key: candidate[key] for key in ("poiId", "name", "address", "distance")
            }
        }
        self.page._location_applied_success(result)

        self.assertTrue(self.page._location_applied)
        self.assertFalse(self.page._declaration_applied)
        self.assertIn("万泉城二区北侧", self.page.location_applied_card.text())
        # 即时地点回读完成后，要等共享任务真正结束才恢复其它设置。
        self.assertFalse(
            self.page.declaration_buttons["无需添加自主声明"].isEnabled()
        )
        self.page._finish_immediate_write("location")
        self.assertTrue(
            self.page.declaration_buttons["无需添加自主声明"].isEnabled()
        )
        self.assertFalse(self.page.to_review_button.isEnabled())
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
        """声明回读失败只落在当前阶段卡，不能暴露原始选择器错误。"""

        diagnostic = "Locator.click: Timeout 5000ms exceeded"
        with patch("ui.douyin_commerce_page.QMessageBox.warning") as warning:
            self.page._immediate_declaration_failed(diagnostic)
        warning.assert_not_called()
        text = self.page._stage_error_labels["declaration"].text()
        self.assertIn("失败原因：", text)
        self.assertIn("下一步：", text)
        self.assertNotIn(diagnostic, text)

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
        info.assert_not_called()


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
    def test_progress_event_only_exposes_phase_label_and_state(self) -> None:
        event = douyin_commerce_session.CommerceProgressEvent(
            phase="uploading_video",
            label="正在上传视频",
        )

        self.assertEqual(
            event.to_public_dict(),
            {
                "phase": "uploading_video",
                "label": "正在上传视频",
                "state": "running",
            },
        )

    def test_login_required_result_has_no_session_or_raw_diagnostic(self) -> None:
        manager = douyin_commerce_session.DouyinCommerceSessionManager()

        result = manager._login_required_result()

        self.assertEqual(result["status"], "needs_login")
        self.assertEqual(result["message"], "登录已失效，请到账号管理重新登录")
        self.assertNotIn("sessionId", result)
        self.assertNotIn("Locator", str(result))

    def test_session_manager_defaults_upload_context_to_background(self) -> None:
        """上传会话默认后台；显式 false 才允许兼容旧调用。"""

        manager = douyin_commerce_session.DouyinCommerceSessionManager()
        background_mode = getattr(manager, "_background_upload_mode", None)
        self.assertTrue(callable(background_mode))
        self.assertTrue(background_mode({}))
        self.assertTrue(background_mode({"backgroundMode": True}))
        self.assertFalse(background_mode({"backgroundMode": False}))

    def test_background_upload_uses_true_headless_browser(self) -> None:
        """默认后台上传不创建可最小化或前置的浏览器窗口。"""

        manager = douyin_commerce_session.DouyinCommerceSessionManager()
        options = getattr(manager, "_commerce_browser_launch_options", None)

        self.assertTrue(callable(options))
        self.assertEqual(
            options({}),
            {"headless": True, "hide_until_ready": False},
        )
        self.assertEqual(
            options({"backgroundMode": True}),
            {"headless": True, "hide_until_ready": False},
        )
        self.assertEqual(
            options({"backgroundMode": False}),
            {"headless": False, "hide_until_ready": False},
        )

    def test_background_upload_minimizes_the_window_after_page_creation(self) -> None:
        """离屏参数不可靠时，必须用 CDP 确认窗口已最小化。"""

        class Session:
            def __init__(self) -> None:
                self.send = AsyncMock(side_effect=[{"windowId": 71}, {}])

        class Context:
            def __init__(self, session) -> None:
                self.session = session
                self.new_cdp_session = AsyncMock(return_value=session)

        class Page:
            def __init__(self, context) -> None:
                self.context = context

        session = Session()
        page = Page(Context(session))

        hidden = asyncio.run(base_social_media.hide_page_window(page))

        self.assertTrue(hidden)
        session.send.assert_has_awaits(
            [
                call("Browser.getWindowForTarget"),
                call(
                    "Browser.setWindowBounds",
                    {"windowId": 71, "bounds": {"windowState": "minimized"}},
                ),
            ]
        )

    def test_hidden_launch_requests_minimized_window_before_page_creation(self) -> None:
        """CDP 连接前先请求最小化，减少 macOS 窗口闪现的机会。"""

        class Chromium:
            def __init__(self) -> None:
                self.launch = AsyncMock(return_value=object())

        class Playwright:
            def __init__(self) -> None:
                self.chromium = Chromium()

        playwright = Playwright()
        asyncio.run(
            base_social_media.launch_chromium_with_codecs(
                playwright,
                force_bundled=True,
                hide_until_ready=True,
            )
        )

        args = playwright.chromium.launch.await_args.kwargs["args"]
        self.assertIn("--start-minimized", args)
        self.assertIn("--window-position=-32000,-32000", args)

    def test_schedule_sync_updates_editor_only_when_platform_time_changed(self) -> None:
        """进入检查前先读平台定时；不一致时写入并二次回读。"""

        class OpenPage:
            def is_closed(self) -> bool:
                return False

        class Uploader:
            _schedule_time_matches = staticmethod(DouYinVideo._schedule_time_matches)

            def __init__(self) -> None:
                self.read_schedule_time_douyin = AsyncMock(
                    side_effect=["2026-08-10 09:00", "2026-08-11 10:00"]
                )
                self.set_schedule_time_douyin = AsyncMock(
                    return_value="2026-08-11 10:00"
                )

        upload_payload = {
            "accountList": ["oneclick_3_demo.json"],
            "fileList": ["/tmp/demo.mp4"],
            "title": "抖音带货定时同步测试",
            "description": "仅验证检查前的定时同步。",
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
        target = "2026-08-11 10:00"
        payload = {
            **upload_payload,
            "selectedMusic": music,
            "locationPoi": location,
            "locationScope": "domestic",
            "contentDeclaration": "内容由AI生成",
            "enableTimer": True,
            "scheduleTime": target,
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
            selected_music=music,
            location=location,
            location_scope="domestic",
            selected_declaration="内容由AI生成",
        )

        result = asyncio.run(manager._sync_schedule("session-demo", payload))

        uploader.set_schedule_time_douyin.assert_awaited_once()
        self.assertEqual(uploader.read_schedule_time_douyin.await_count, 2)
        self.assertEqual(result, {"status": "updated", "scheduledAt": target})
        self.assertEqual(manager._session.schedule_time, target)

    def test_schedule_sync_clears_platform_timer_when_user_selects_immediate(self) -> None:
        """已设置定时后切回立即发表，必须清除平台定时并二次回读。"""

        class OpenPage:
            def is_closed(self) -> bool:
                return False

        class Uploader:
            _schedule_time_matches = staticmethod(DouYinVideo._schedule_time_matches)

            def __init__(self) -> None:
                self.read_schedule_time_douyin = AsyncMock(
                    side_effect=["2026-08-11 10:00", ""]
                )
                self.clear_schedule_time_douyin = AsyncMock(return_value="")

        upload_payload = {
            "accountList": ["oneclick_3_demo.json"],
            "fileList": ["/tmp/demo.mp4"],
            "title": "抖音带货取消定时测试",
            "description": "仅验证检查前取消平台定时。",
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
            selected_music=music,
            location=location,
            location_scope="domestic",
            selected_declaration="内容由AI生成",
            schedule_time="2026-08-11 10:00",
        )

        result = asyncio.run(manager._sync_schedule("session-demo", payload))

        uploader.clear_schedule_time_douyin.assert_awaited_once()
        self.assertEqual(uploader.read_schedule_time_douyin.await_count, 2)
        self.assertEqual(result, {"status": "updated", "scheduledAt": None})
        self.assertEqual(manager._session.schedule_time, "")

    def test_schedule_cancel_selects_immediate_publish_and_reads_blank_state(self) -> None:
        """执行器必须点击实际的“立即发布”，并确认“定时发布”已取消。"""

        class Radio:
            def __init__(self, text: str, checked: bool = False) -> None:
                self.text = text
                self.checked = checked
                self.peer: "Radio | None" = None

            async def is_visible(self) -> bool:
                return True

            async def inner_text(self) -> str:
                return self.text

            async def get_attribute(self, name: str) -> str:
                self.attribute_name = name
                return "true" if self.checked else "false"

            async def click(self, *, timeout: int) -> None:
                self.timeout = timeout
                self.checked = True
                if self.peer is not None:
                    self.peer.checked = False

        class Labels:
            def __init__(self, rows) -> None:
                self.rows = rows

            async def count(self) -> int:
                return len(self.rows)

            def nth(self, index: int):
                return self.rows[index]

        class Page:
            def __init__(self) -> None:
                self.immediate = Radio("立即发布")
                self.scheduled = Radio("定时发布", checked=True)
                self.immediate.peer = self.scheduled
                self.scheduled.peer = self.immediate
                self.labels = Labels([self.immediate, self.scheduled])

            def locator(self, selector: str):
                self.selector = selector
                return self.labels

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        page = Page()
        uploader = object.__new__(DouYinVideo)
        uploader.schedule_verification = "2026-08-11 10:00"

        result = asyncio.run(uploader.clear_schedule_time_douyin(page))

        self.assertEqual(result, "")
        self.assertTrue(page.immediate.checked)
        self.assertFalse(page.scheduled.checked)
        self.assertEqual(uploader.schedule_verification, "")

    def test_login_detection_requires_an_explicit_login_semantic(self) -> None:
        class Body:
            def __init__(self, text: str) -> None:
                self.text = text

            async def inner_text(self, *, timeout: int) -> str:
                self.timeout = timeout
                return self.text

        class Page:
            url = "https://creator.douyin.com/creator-micro/content/post/video"

            def __init__(self, text: str) -> None:
                self.body = Body(text)

            def locator(self, selector: str) -> Body:
                self.selector = selector
                return self.body

        self.assertFalse(
            asyncio.run(
                douyin_commerce_session.DouyinCommerceSessionManager._is_login_required_page(
                    Page("创作者中心 作品管理 当前账号正常")
                )
            )
        )
        self.assertTrue(
            asyncio.run(
                douyin_commerce_session.DouyinCommerceSessionManager._is_login_required_page(
                    Page("请使用抖音扫码登录")
                )
            )
        )

    def test_declaration_can_be_written_before_music_and_location(self) -> None:
        """声明弹层不是音乐或定位的后置条件，应允许从已上传编辑页独立写入。"""

        class OpenPage:
            def is_closed(self) -> bool:
                return False

        class Uploader:
            set_content_declaration = AsyncMock(return_value="无需添加自主声明")

        manager = douyin_commerce_session.DouyinCommerceSessionManager()
        uploader = Uploader()
        manager._session = douyin_commerce_session._CommerceEditorSession(
            session_id="session-demo",
            upload_payload={},
            account_name="测试账号",
            browser=None,
            context=None,
            page=OpenPage(),
            playwright=None,
            uploader=uploader,
            stage="uploaded",
        )

        result = asyncio.run(
            manager._select_content_declaration("session-demo", "无需添加自主声明")
        )

        self.assertEqual(result, "无需添加自主声明")
        uploader.set_content_declaration.assert_awaited_once()
        self.assertEqual(manager._session.selected_declaration, "无需添加自主声明")

    def test_location_search_can_start_before_music_selection(self) -> None:
        """用户可先搜索地点；仅音乐抽屉实际打开时才阻止并发页面操作。"""

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
            stage="uploaded",
            selected_declaration="无需添加自主声明",
        )
        expected = [
            {
                "poiId": "poi-a",
                "name": "北海银滩景区",
                "address": "广西壮族自治区北海市银海区银滩大道中段",
                "distance": "6.0km",
            }
        ]
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
        self.assertEqual(manager._session.selected_declaration, "无需添加自主声明")

    def test_uploader_progress_callback_exposes_only_public_phase(self) -> None:
        app = object.__new__(DouYinVideo)
        events: list[dict[str, str]] = []
        app.progress_callback = events.append

        app._report_commerce_progress("waiting_platform", "正在等待平台处理")

        self.assertEqual(
            events,
            [
                {
                    "phase": "waiting_platform",
                    "label": "正在等待平台处理",
                    "state": "running",
                }
            ],
        )

    def test_immediate_preflight_reads_but_does_not_write_schedule_controls(self) -> None:
        class OpenPage:
            def is_closed(self) -> bool:
                return False

        class Uploader:
            _schedule_time_matches = staticmethod(DouYinVideo._schedule_time_matches)

            def __init__(self) -> None:
                self.location_verification = "北海银滩景区"
                self.read_schedule_time_douyin = AsyncMock(return_value="")
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
        uploader.read_schedule_time_douyin.assert_awaited_once()
        uploader.verify_prepublish_form.assert_awaited_once()
        self.assertFalse(result["scheduled"])
        self.assertIsNone(result["scheduledAt"])
        self.assertEqual(manager._session.schedule_time, "")

    def test_headless_immediate_submit_does_not_reveal_browser_window(self) -> None:
        """默认无头会话最终提交也不能把浏览器窗口带到用户前台。"""
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
                self.background_mode_at_receipt = None

                async def wait_for_receipt(_page):
                    self.background_mode_at_receipt = base_social_media.is_publish_background_mode()
                    return {"status": "published"}

                self._wait_formal_publish_result = AsyncMock(side_effect=wait_for_receipt)

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

        reveal.assert_not_awaited()
        self.assertTrue(uploader.background_mode_at_receipt)
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
            "标题、文案或标签",
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

    def test_content_sync_updates_fields_without_creating_a_new_upload_session(self) -> None:
        """同一账号和视频只同步内容，保留已回读的平台设置。"""

        class OpenPage:
            def is_closed(self) -> bool:
                return False

        class Uploader:
            sync_uploaded_editor_content = AsyncMock(
                return_value={
                    "title": "修改后的标题",
                    "description": "修改后的文案",
                    "tags": ["北海", "团购"],
                    "form": {"title_confirmed": True},
                }
            )

        upload_payload = {
            "accountList": ["oneclick_3_demo.json"],
            "fileList": ["/tmp/demo.mp4"],
            "title": "原标题",
            "description": "原文案",
            "tags": ["北海"],
        }
        music = {"musicId": "music-001", "title": "收藏音乐"}
        location = {
            "poiId": "poi-001",
            "name": "北海银滩景区",
            "address": "广西壮族自治区北海市银海区银滩大道中段",
        }
        manager = douyin_commerce_session.DouyinCommerceSessionManager()
        uploader = Uploader()
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
            selected_declaration="无需添加自主声明",
            preflight_fingerprint="旧预检",
            schedule_time="2026-08-06 09:00",
        )
        payload = {
            **upload_payload,
            "title": "修改后的标题",
            "description": "修改后的文案",
            "tags": ["北海", "团购"],
        }

        synchronize = getattr(manager, "_synchronize_content", None)
        self.assertTrue(callable(synchronize))
        result = asyncio.run(synchronize("session-demo", payload))

        uploader.sync_uploaded_editor_content.assert_awaited_once_with(
            manager._session.page,
            title="修改后的标题",
            description="修改后的文案",
            tags=["北海", "团购"],
        )
        self.assertEqual(result["status"], "synced")
        self.assertEqual(manager._session.upload_payload["title"], "修改后的标题")
        self.assertEqual(manager._session.upload_payload["description"], "修改后的文案")
        self.assertEqual(manager._session.upload_payload["tags"], ["北海", "团购"])
        self.assertEqual(manager._session.preflight_fingerprint, "")
        self.assertEqual(manager._session.selected_music, music)
        self.assertEqual(manager._session.location, location)
        self.assertEqual(manager._session.selected_declaration, "无需添加自主声明")
        self.assertEqual(manager._session.schedule_time, "2026-08-06 09:00")

    def test_content_sync_rejects_changed_account_or_video_before_editor_write(self) -> None:
        """账号或视频改变必须走新上传，不能在旧编辑页误写内容。"""

        class OpenPage:
            def is_closed(self) -> bool:
                return False

        class Uploader:
            sync_uploaded_editor_content = AsyncMock()

        upload_payload = {
            "accountList": ["oneclick_3_demo.json"],
            "fileList": ["/tmp/demo.mp4"],
            "title": "原标题",
            "description": "原文案",
            "tags": [],
        }
        manager = douyin_commerce_session.DouyinCommerceSessionManager()
        uploader = Uploader()
        manager._session = douyin_commerce_session._CommerceEditorSession(
            session_id="session-demo",
            upload_payload=upload_payload,
            account_name="测试账号",
            browser=None,
            context=None,
            page=OpenPage(),
            playwright=None,
            uploader=uploader,
        )

        synchronize = getattr(manager, "_synchronize_content", None)
        self.assertTrue(callable(synchronize))
        changed_account = dict(upload_payload, accountList=["oneclick_3_other.json"])
        with self.assertRaisesRegex(
            douyin_commerce_session.DouyinCommerceSessionError,
            "账号或视频",
        ):
            asyncio.run(synchronize("session-demo", changed_account))
        uploader.sync_uploaded_editor_content.assert_not_awaited()


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
