# -*- coding: utf-8 -*-
"""抖音带货首版的离线边界与桌面页面回归测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock, call, patch
from zoneinfo import ZoneInfo

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QDate, Qt
from PyQt6.QtGui import QColor, QImage, QInputMethodEvent
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFrame,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
)
from loguru import logger
from playwright.async_api import async_playwright

from app_core import (
    database,
    douyin_commerce_batch_draft_service,
    douyin_commerce_collectors,
    douyin_commerce_service,
    douyin_commerce_session,
    douyin_favorite_music_cache,
    douyin_music_service,
    douyin_publish_executor,
    media_service,
    publish_service,
    task_service,
)
from uploader.douyin_uploader.main import DouYinVideo
from app_core import douyin_verification
from app_core.douyin_commerce_batch_executor import DouyinCommerceBatchExecutor
from app_core.douyin_verification import DouyinVerificationBroker, VerificationChallenge
from ui.background_task import BackgroundTask, BackgroundTaskRunner
from ui.common import apply_style
from ui.douyin_commerce_page import (
    DouyinCommerceBatchResumeConfirmDialog,
    DouyinCommercePage,
    _ImeAwarePlainTextEdit,
)
from ui.runtime_log import runtime_log_bus
from utils import base_social_media
from test_douyin_commerce_batch_executor import FakeCommerceSessionManager


class BackgroundTaskRunnerLifecycleTests(unittest.TestCase):
    """同键任务替换时，取消的旧 worker 不得回调新 UI 世代。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    class _QueuedPool:
        """保留真实 BackgroundTask，由测试确定性控制运行顺序。"""

        def __init__(self) -> None:
            self.tasks: list[BackgroundTask] = []

        def start(self, task: BackgroundTask) -> None:
            self.tasks.append(task)

    def setUp(self) -> None:
        self.pool = self._QueuedPool()
        self.runner = BackgroundTaskRunner()
        self.runner.pool = self.pool

    def test_cancelled_old_task_cannot_finish_replacement_with_same_key(self) -> None:
        """旧 worker 迟到 finished 不得触发旧业务回调或清理同键新任务。"""

        old_finished: list[str] = []
        new_finished: list[str] = []
        self.assertTrue(
            self.runner.run(
                "shared-key",
                fn=lambda: "old",
                on_finished=lambda: old_finished.append("old"),
            )
        )
        old_task = self.pool.tasks[-1]

        self.assertTrue(self.runner.cancel_pending("shared-key"))
        self.assertTrue(
            self.runner.run(
                "shared-key",
                fn=lambda: "new",
                on_finished=lambda: new_finished.append("new"),
            )
        )
        new_task = self.pool.tasks[-1]

        old_task.run()
        QApplication.processEvents()

        self.assertEqual(old_finished, [])
        self.assertEqual(new_finished, [])
        self.assertIs(self.runner.active.get("shared-key"), new_task)

        new_task.run()
        QApplication.processEvents()

        self.assertEqual(old_finished, [])
        self.assertEqual(new_finished, ["new"])
        self.assertFalse(self.runner.is_running("shared-key"))

    def test_cancelled_task_finishes_internally_without_business_callback(self) -> None:
        """无替换任务时，取消的 worker 仍终态可等待，但不执行旧 UI 回调。"""

        business_finished: list[str] = []
        self.assertTrue(
            self.runner.run(
                "cancelled-key",
                fn=lambda: "unused",
                on_finished=lambda: business_finished.append("finished"),
            )
        )
        old_task = self.pool.tasks[-1]

        self.assertTrue(self.runner.cancel_pending("cancelled-key"))
        old_task.run()
        QApplication.processEvents()

        self.assertTrue(old_task.wait_for_finished(0))
        self.assertTrue(self.runner.wait_for_finished("cancelled-key", 0))
        self.assertEqual(business_finished, [])
        self.assertFalse(self.runner.is_running("cancelled-key"))


class DouyinImePlaceholderTests(unittest.TestCase):
    """中文输入法预编辑期间，文案提示不得与候选文字重叠。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.editor = _ImeAwarePlainTextEdit()
        self.editor.setPlaceholderText("填写视频发布文案")

    def test_hides_placeholder_during_ime_preedit(self) -> None:
        event = QInputMethodEvent("chi", [])

        QApplication.sendEvent(self.editor, event)

        self.assertEqual(self.editor.placeholderText(), "")
        self.assertEqual(self.editor.toPlainText(), "")

    def test_restores_placeholder_after_cancelled_preedit_when_empty(self) -> None:
        QApplication.sendEvent(self.editor, QInputMethodEvent("chi", []))
        QApplication.sendEvent(self.editor, QInputMethodEvent("", []))

        self.assertEqual(self.editor.placeholderText(), "填写视频发布文案")

    def test_keeps_placeholder_hidden_after_ime_commit(self) -> None:
        event = QInputMethodEvent("", [])
        event.setCommitString("测试")

        QApplication.sendEvent(self.editor, event)

        self.assertEqual(self.editor.toPlainText(), "测试")
        self.assertEqual(self.editor.placeholderText(), "")

        self.editor.clear()

        self.assertEqual(self.editor.placeholderText(), "填写视频发布文案")


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

    def test_title_and_tags_are_optional_when_description_is_present(self) -> None:
        checked = douyin_commerce_service.validate_douyin_commerce_upload_payload(
            dict(self.payload, title="", tags=[])
        )

        self.assertEqual(checked["title"], "")
        self.assertEqual(checked.get("tags"), [])

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
                self.evaluate = AsyncMock(side_effect=["", "北海夜南香"])

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
        self.assertEqual(mode.await_count, 3)
        self.assertEqual(
            open_search.await_args_list,
            [
                call(page, mode.return_value),
                call(page, mode.return_value),
                call(page, mode.return_value),
            ],
        )
        set_scope.assert_awaited_once_with(page, "domestic")
        self.assertEqual(
            field.fill.await_args_list,
            [call("", timeout=8_000), call("北海夜南香", timeout=8_000)],
        )
        self.assertEqual(result[0]["name"], row["name"])
        self.assertNotIn("commerceStore", result[0])

    def test_location_search_stops_when_old_keyword_cannot_be_cleared(self) -> None:
        """平台仍回读旧关键词时，禁止继续输入新词或读取旧候选。"""

        class SearchInput:
            scroll_into_view_if_needed = AsyncMock()
            click = AsyncMock()
            fill = AsyncMock()
            evaluate = AsyncMock(return_value="上一次关键词")

        field = SearchInput()

        class Page:
            wait_for_timeout = AsyncMock()

        with patch.object(
            douyin_commerce_service,
            "_ensure_position_tag",
            new_callable=AsyncMock,
        ), patch.object(
            douyin_commerce_service,
            "_ensure_local_group_buy_mode",
            new_callable=AsyncMock,
            return_value=object(),
        ), patch.object(
            douyin_commerce_service,
            "_open_commerce_search_input",
            new_callable=AsyncMock,
            return_value=field,
        ), patch.object(
            douyin_commerce_service,
            "set_commerce_location_scope",
            new_callable=AsyncMock,
        ), patch.object(
            douyin_commerce_service,
            "_wait_for_fresh_commerce_location_results",
            new_callable=AsyncMock,
        ) as wait_results:
            with self.assertRaisesRegex(
                douyin_commerce_service.DouyinCommerceError,
                "未能清空旧关键词",
            ):
                asyncio.run(
                    douyin_commerce_service.search_commerce_location_store_candidates(
                        Page(),
                        "夜心数码",
                        scope="local",
                    )
                )

        self.assertEqual(field.fill.await_args_list, [call("", timeout=8_000)])
        wait_results.assert_not_awaited()

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
        with patch.object(
            douyin_commerce_service,
            "_visible_commerce_search_input",
            new_callable=AsyncMock,
            return_value=object(),
        ):
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

    def test_location_scope_waits_through_transient_missing_pair_after_reopen(self) -> None:
        """重复或切换搜索重开面板时，短暂 0/0 不是永久结构错误。"""

        class Page:
            def __init__(self) -> None:
                self.wait_for_timeout = AsyncMock()
                self.evaluate = AsyncMock(
                    side_effect=[
                        {
                            "state": "ambiguous",
                            "reason": "pair-not-in-search-panel",
                            "local": 0,
                            "domestic": 0,
                        },
                        {"state": "selected", "local": 1, "domestic": 1},
                        {"state": "selected", "local": 1, "domestic": 1},
                    ]
                )

        page = Page()
        with patch.object(
            douyin_commerce_service,
            "_visible_commerce_search_input",
            new_callable=AsyncMock,
            return_value=object(),
        ):
            result = asyncio.run(
                douyin_commerce_service.set_commerce_location_scope(page, "domestic")
            )

        self.assertEqual(result, "国内")
        page.wait_for_timeout.assert_any_await(200)

    def test_location_scope_allows_slow_platform_panel_to_materialize(self) -> None:
        """连续搜索被平台限速时，范围标签可在约三秒后才重新挂载。"""

        missing = {
            "state": "ambiguous",
            "reason": "pair-not-in-search-panel",
            "local": 0,
            "domestic": 0,
        }

        class Page:
            def __init__(self) -> None:
                self.wait_for_timeout = AsyncMock()
                self.evaluate = AsyncMock(
                    side_effect=[missing] * 15
                    + [
                        {"state": "selected", "local": 1, "domestic": 1},
                        {"state": "selected", "local": 1, "domestic": 1},
                    ]
                )

        page = Page()
        with patch.object(
            douyin_commerce_service,
            "_visible_commerce_search_input",
            new_callable=AsyncMock,
            return_value=object(),
        ):
            result = asyncio.run(
                douyin_commerce_service.set_commerce_location_scope(page, "domestic")
            )

        self.assertEqual(result, "国内")
        self.assertEqual(page.evaluate.await_count, 17)

    def test_location_scope_reacquires_search_input_after_platform_rerender(self) -> None:
        """切换本地/国内会重绘地点面板，必须重新标记新输入框后再回读选中态。"""

        class Control:
            def __init__(self, page) -> None:
                self.page = page

            async def click(self, **_kwargs) -> None:
                self.page.scope_selected = True
                # 真实页面在切换范围后会替换输入框节点，旧 marker 随之消失。
                self.page.input_marked = False

        class Page:
            def __init__(self) -> None:
                self.input_marked = False
                self.scope_selected = False
                self.selected_reads = 0
                self.control = Control(self)
                self.wait_for_timeout = AsyncMock()

            async def evaluate(self, script: str):
                if "const editableSelector" in script:
                    self.input_marked = True
                    return {"count": 1, "source": "portal"}
                if "scope-selection-not-exclusive" in script:
                    if not self.input_marked:
                        return {
                            "state": "ambiguous",
                            "reason": "search-input-not-visible",
                            "local": 0,
                            "domestic": 0,
                        }
                    if not self.scope_selected:
                        return {"state": "ready", "local": 1, "domestic": 1}
                    self.selected_reads += 1
                    return {"state": "selected", "local": 1, "domestic": 1}
                raise AssertionError("测试未覆盖的页面脚本")

            def locator(self, _selector: str):
                return self.control

        page = Page()

        result = asyncio.run(
            douyin_commerce_service.set_commerce_location_scope(page, "local")
        )

        self.assertEqual(result, "本地")
        self.assertEqual(page.selected_reads, 2)

    def test_location_search_uses_recreated_input_after_scope_switch(self) -> None:
        """范围切换后只能清空并填写重绘后的新输入框。"""

        class SearchInput:
            def __init__(self, name: str) -> None:
                self.name = name
                self.value = ""
                self.scroll_into_view_if_needed = AsyncMock()
                self.click = AsyncMock()
                self.filled: list[str] = []

            async def fill(self, value: str, **_kwargs) -> None:
                self.value = value
                self.filled.append(value)

            async def evaluate(self, _script: str) -> str:
                return self.value

        old_field = SearchInput("old")
        new_field = SearchInput("new")

        class Page:
            wait_for_timeout = AsyncMock()

        row = {
            "name": "北海夜南香",
            "address": "广西壮族自治区北海市银海区银滩大道 1 号",
        }
        mode_controls = [object(), object(), object()]
        with patch.object(
            douyin_commerce_service, "_ensure_position_tag", new_callable=AsyncMock
        ), patch.object(
            douyin_commerce_service,
            "_ensure_local_group_buy_mode",
            new_callable=AsyncMock,
            side_effect=mode_controls,
        ) as mode, patch.object(
            douyin_commerce_service,
            "_open_commerce_search_input",
            new_callable=AsyncMock,
            side_effect=[old_field, new_field, new_field],
        ) as open_search, patch.object(
            douyin_commerce_service,
            "set_commerce_location_scope",
            new_callable=AsyncMock,
            return_value="本地",
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
                    Page(), "北海夜南香", scope="local"
                )
            )

        self.assertEqual(result[0]["name"], "北海夜南香")
        self.assertEqual(mode.await_count, 3)
        self.assertEqual(open_search.await_count, 3)
        self.assertEqual(old_field.filled, [])
        self.assertEqual(new_field.filled, ["", "北海夜南香"])

    def test_location_search_switches_page_scope_before_entering_keyword(self) -> None:
        """平台在输入关键词后立刻检索，因此范围切换必须先于 fill。"""

        events: list[str] = []

        class SearchInput:
            def __init__(self) -> None:
                self.value = ""

            async def scroll_into_view_if_needed(self, **_kwargs) -> None:
                events.append("scroll")

            async def click(self, **_kwargs) -> None:
                events.append("click")

            async def fill(self, value: str, **_kwargs) -> None:
                self.value = value
                events.append(f"fill:{value}")

            async def evaluate(self, _script: str) -> str:
                return self.value

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

        self.assertEqual(mode.await_count, 3)
        self.assertEqual(result[0]["name"], "北海夜南香")
        self.assertEqual(
            events,
            [
                "scroll",
                "click",
                "scope:domestic",
                "scroll",
                "click",
                "fill:",
                "wait:450",
                "scroll",
                "click",
                "fill:北海夜南香",
            ],
        )

    def test_location_search_materializes_scope_panel_when_tabs_are_initially_absent(self) -> None:
        """新版范围标签延迟渲染时，先用本次关键词唤起面板再切换并重搜。"""

        class SearchInput:
            def __init__(self) -> None:
                self.value = ""
                self.fill = AsyncMock(side_effect=self._fill)
                self.scroll_into_view_if_needed = AsyncMock()
                self.click = AsyncMock()

            async def _fill(self, value: str, **_kwargs) -> None:
                self.value = value

            async def evaluate(self, _script: str) -> str:
                return self.value

        field = SearchInput()

        class Page:
            wait_for_timeout = AsyncMock()

        missing_scope = douyin_commerce_service.DouyinCommerceError(
            "抖音位置搜索范围“本地/国内”控件未能唯一显示"
            "（当前地点面板内本地 0 个、国内 0 个；pair-not-in-search-panel），已安全停止"
        )
        row = {
            "name": "北海夜南香",
            "address": "广西壮族自治区北海市银海区银滩大道 1 号",
        }
        with patch.object(
            douyin_commerce_service, "_ensure_position_tag", new_callable=AsyncMock
        ), patch.object(
            douyin_commerce_service,
            "_ensure_local_group_buy_mode",
            new_callable=AsyncMock,
            return_value=object(),
        ), patch.object(
            douyin_commerce_service,
            "_open_commerce_search_input",
            new_callable=AsyncMock,
            return_value=field,
        ), patch.object(
            douyin_commerce_service,
            "set_commerce_location_scope",
            new_callable=AsyncMock,
            side_effect=[missing_scope, "国内"],
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
                    Page(), "北海夜南香", scope="domestic"
                )
            )

        self.assertEqual(set_scope.await_count, 2)
        self.assertEqual(field.click.await_count, 4)
        self.assertEqual(
            field.fill.await_args_list,
            [
                call("", timeout=8_000),
                call("北海夜南香", timeout=8_000),
                call("", timeout=8_000),
                call("北海夜南香", timeout=8_000),
            ],
        )
        self.assertEqual(result[0]["name"], "北海夜南香")

    def test_location_search_reacquires_input_after_clear_triggers_rerender(self) -> None:
        """清空关键词触发输入框替换后，必须重新定位新节点再填写。"""

        class SearchInput:
            def __init__(self, *, reject_nonempty: bool = False) -> None:
                self.value = ""
                self.reject_nonempty = reject_nonempty
                self.fill = AsyncMock(side_effect=self._fill)
                self.scroll_into_view_if_needed = AsyncMock()
                self.click = AsyncMock()

            async def _fill(self, value: str, **_kwargs) -> None:
                if value and self.reject_nonempty:
                    raise RuntimeError("detached input")
                self.value = value

            async def evaluate(self, _script: str) -> str:
                return self.value

        initial = SearchInput()
        stale_after_scope = SearchInput(reject_nonempty=True)
        fresh_after_clear = SearchInput()

        class Page:
            wait_for_timeout = AsyncMock()

        row = {
            "name": "北海夜南香",
            "address": "广西壮族自治区北海市银海区银滩大道 1 号",
        }
        with patch.object(
            douyin_commerce_service, "_ensure_position_tag", new_callable=AsyncMock
        ), patch.object(
            douyin_commerce_service,
            "_ensure_local_group_buy_mode",
            new_callable=AsyncMock,
            return_value=object(),
        ) as mode, patch.object(
            douyin_commerce_service,
            "_open_commerce_search_input",
            new_callable=AsyncMock,
            side_effect=[initial, stale_after_scope, fresh_after_clear],
        ) as open_input, patch.object(
            douyin_commerce_service,
            "set_commerce_location_scope",
            new_callable=AsyncMock,
            return_value="国内",
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
                    Page(), "北海夜南香", scope="domestic"
                )
            )

        self.assertEqual(mode.await_count, 3)
        self.assertEqual(open_input.await_count, 3)
        stale_after_scope.fill.assert_awaited_once_with("", timeout=8_000)
        fresh_after_clear.fill.assert_awaited_once_with("北海夜南香", timeout=8_000)
        self.assertEqual(result[0]["name"], "北海夜南香")

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

    def test_location_search_accepts_stable_keyword_results_after_scope_switch(self) -> None:
        """范围切换已触发检索时，同一组稳定匹配候选不应被误判为旧列表。"""

        rows = [
            {
                "name": "夜南香北京烤鸭(万泉城店)",
                "address": "广西壮族自治区北海市银海区银滩大道万泉城二区北门",
            }
        ]

        class Page:
            wait_for_timeout = AsyncMock()

        signature = douyin_commerce_service._location_result_signature(rows)
        with patch.object(
            douyin_commerce_service,
            "_visible_commerce_location_result_snapshot",
            new_callable=AsyncMock,
            side_effect=[(object(), rows, signature), (object(), rows, signature)],
        ):
            _listbox, result = asyncio.run(
                douyin_commerce_service._wait_for_fresh_commerce_location_results(
                    Page(),
                    baseline_signature=signature,
                    keyword="夜南香",
                    allow_stable_baseline_match=True,
                )
            )

        self.assertEqual(result, rows)

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

        with patch.object(
            douyin_commerce_service,
            "_visible_commerce_search_input",
            new_callable=AsyncMock,
            return_value=object(),
        ), self.assertRaisesRegex(
            douyin_commerce_service.DouyinCommerceError,
            "当前地点面板内本地 2 个、国内 1 个；pair-not-in-search-panel",
        ):
            asyncio.run(douyin_commerce_service.set_commerce_location_scope(Page(), "国内"))

    def test_position_tag_waits_for_unique_dynamic_entry_before_opening_add_tag(self) -> None:
        page = MagicMock()
        page.wait_for_timeout = AsyncMock()
        tag_select = MagicMock()
        position_option = MagicMock()
        position_option.click = AsyncMock()
        missing_anchor = douyin_commerce_service.DouyinCommerceError(
            "抖音页面未找到唯一可用的带货模式控件组（实际 0 个），已安全停止"
        )
        with patch.object(
            douyin_commerce_service,
            "_anchor_controls",
            new_callable=AsyncMock,
            side_effect=[
                missing_anchor,
                (object(), object(), "带货模式", ""),
            ],
        ), patch.object(
            douyin_commerce_service,
            "_mark_unique_position_tag_select",
            new_callable=AsyncMock,
            side_effect=[None, None, tag_select],
        ), patch.object(
            douyin_commerce_service,
            "_open_unique_add_tag",
            new_callable=AsyncMock,
        ) as open_add_tag, patch.object(
            douyin_commerce_service,
            "_open_exact_select",
            new_callable=AsyncMock,
        ), patch.object(
            douyin_commerce_service,
            "_wait_position_tag_option",
            new_callable=AsyncMock,
            return_value=position_option,
        ):
            asyncio.run(douyin_commerce_service._ensure_position_tag(page))

        open_add_tag.assert_not_awaited()
        self.assertEqual(
            page.wait_for_timeout.await_args_list[:2],
            [call(150), call(150)],
        )
        position_option.click.assert_awaited_once_with(timeout=8_000)

    def test_add_tag_may_directly_render_css_position_row_without_option_menu(self) -> None:
        """平台直接渲染位置输入行时，应立即回读成功而不是继续等菜单项。"""

        page = MagicMock()
        page.wait_for_timeout = AsyncMock()
        missing_anchor = douyin_commerce_service.DouyinCommerceError(
            "抖音页面未找到唯一可用的带货模式控件组（实际 0 个），已安全停止"
        )
        with patch.object(
            douyin_commerce_service,
            "_anchor_controls",
            new_callable=AsyncMock,
            side_effect=[missing_anchor, (object(), object(), "", "")],
        ), patch.object(
            douyin_commerce_service,
            "_wait_unique_position_tag_select",
            new_callable=AsyncMock,
            return_value=None,
        ), patch.object(
            douyin_commerce_service,
            "_open_unique_add_tag",
            new_callable=AsyncMock,
            return_value=True,
        ), patch.object(
            douyin_commerce_service,
            "_open_exact_select",
            new_callable=AsyncMock,
        ) as open_select, patch.object(
            douyin_commerce_service,
            "_wait_position_tag_option",
            new_callable=AsyncMock,
        ) as wait_option:
            asyncio.run(douyin_commerce_service._ensure_position_tag(page))

        open_select.assert_not_awaited()
        wait_option.assert_not_awaited()

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

    def test_location_entry_diagnostic_writes_only_allowlisted_structure(self) -> None:
        """诊断快照不得落盘输入值、正文、Cookie、HTML 或请求数据。"""

        class Page:
            def __init__(self) -> None:
                self.evaluate = AsyncMock(
                    return_value={
                        "schemaVersion": 1,
                        "kind": "douyin-location-entry-structure",
                        "labelCounts": {"位置": 1, "带货模式": 1, "正文": 99},
                        "landmarks": [
                            {
                                "label": "位置",
                                "leaf": {
                                    "tag": "span",
                                    "role": "",
                                    "classes": ["position-label"],
                                    "ariaLabel": "位置",
                                    "placeholder": "",
                                    "rect": {"x": 10, "y": 20, "width": 30, "height": 40},
                                    "tabIndex": -1,
                                    "disabled": False,
                                    "readOnly": False,
                                    "contentEditable": False,
                                    "dataAttributes": ["data-state"],
                                    "state": {"ariaExpanded": "false"},
                                    "value": "不允许保存",
                                    "outerHTML": "<span>页面正文</span>",
                                },
                                "ancestors": [],
                                "siblings": [],
                                "text": "页面正文",
                            }
                        ],
                        "editableNodes": [
                            {
                                "tag": "input",
                                "role": "textbox",
                                "classes": ["location-input"],
                                "ariaLabel": "",
                                "placeholder": "输入地理位置",
                                "rect": {"x": 50, "y": 60, "width": 200, "height": 32},
                                "tabIndex": 0,
                                "disabled": False,
                                "readOnly": False,
                                "contentEditable": False,
                                "dataAttributes": [],
                                "state": {},
                                "value": "夜心数码",
                            }
                        ],
                        "cookie": "secret",
                        "request": {"url": "secret"},
                    }
                )

        with tempfile.TemporaryDirectory() as root:
            path = asyncio.run(
                douyin_commerce_service._capture_location_entry_diagnostic(
                    Page(), Path(root)
                )
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(payload["labelCounts"], {"位置": 1, "带货模式": 1})
        self.assertEqual(payload["landmarks"][0]["label"], "位置")
        self.assertEqual(payload["editableNodes"][0]["placeholder"], "输入地理位置")
        for forbidden in ("夜心数码", "页面正文", "secret", "outerHTML", "cookie", "request", "value"):
            self.assertNotIn(forbidden, rendered)

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

    def test_anchor_controls_accept_modern_position_mode_and_input_surface(self) -> None:
        """新版“位置 + 带货模式 + 输入框”不应退回旧的添加标签入口。"""

        class Page:
            def __init__(self) -> None:
                self.evaluate = AsyncMock(
                    return_value={
                        "count": 1,
                        "mode": "带货模式",
                        "store": "",
                        "surface": "modern-position-mode-input",
                    }
                )
                self.locator = MagicMock(side_effect=lambda selector: selector)

        page = Page()
        mode, store, mode_value, store_value = asyncio.run(
            douyin_commerce_service._anchor_controls(page)
        )

        self.assertEqual(mode, '[data-oneclick-commerce-mode="active"]')
        self.assertEqual(store, '[data-oneclick-commerce-store="active"]')
        self.assertEqual(mode_value, "带货模式")
        self.assertEqual(store_value, "")
        script = page.evaluate.await_args.args[0]
        self.assertIn("modern-position-mode-input", script)
        self.assertIn("modeLeaves", script)
        self.assertIn("'input, textarea", script)

    def test_native_location_input_opens_without_requiring_semi_select_markup(self) -> None:
        """现代位置输入框本身就是入口，不能再按旧 semi-select 结构打开。"""

        class InputControl:
            def __init__(self) -> None:
                self.click = AsyncMock()
                self.evaluate = AsyncMock(return_value=True)

        class Page:
            pass

        control = InputControl()
        listbox = object()
        with patch.object(
            douyin_commerce_service,
            "_visible_store_listbox",
            new_callable=AsyncMock,
            side_effect=[None, listbox],
        ), patch.object(
            douyin_commerce_service,
            "_open_exact_select",
            new_callable=AsyncMock,
        ) as open_select:
            result = asyncio.run(
                douyin_commerce_service._open_store_selector(Page(), control)
            )

        self.assertIs(result, listbox)
        control.click.assert_awaited_once_with(timeout=5_000)
        open_select.assert_not_awaited()

    def test_position_tag_can_open_a_uniquely_marked_modern_control_without_semi_markup(self) -> None:
        """新版位置入口不是 semi-select 时，仍只点击已经唯一确认的入口。"""

        class EmptyChildren:
            async def count(self) -> int:
                return 0

        class Control:
            def __init__(self) -> None:
                self.click = AsyncMock()

            def locator(self, _selector: str):
                return EmptyChildren()

            async def scroll_into_view_if_needed(self, **_kwargs) -> None:
                return None

        control = Control()
        with patch.object(
            douyin_commerce_service,
            "_is_direct_location_entry",
            new_callable=AsyncMock,
            return_value=True,
        ):
            asyncio.run(
                douyin_commerce_service._open_exact_select(
                    object(), control, "位置标签"
                )
            )

        control.click.assert_awaited_once_with(timeout=3_000)

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

    def test_saved_location_is_clicked_in_the_same_open_panel(self) -> None:
        """搜索已匹配时必须直接使用当前 listbox，不得重开面板丢失候选快照。"""

        page = object()
        listbox = object()
        row = {
            "poiId": "visible-poi:target",
            "name": "夜南香北京烤鸭",
            "address": "陕西省安康市汉滨区江北办富民街2号",
        }
        events: list[str] = []

        async def search(*_args, **_kwargs):
            events.append("search")
            return [dict(row)]

        async def current_listbox(_page):
            events.append("listbox")
            return listbox

        async def apply_open(_page, actual_listbox, candidate):
            self.assertIs(actual_listbox, listbox)
            self.assertEqual(
                {key: candidate.get(key) for key in ("poiId", "name", "address")},
                row,
            )
            events.append("click")
            return {"location": dict(row)}

        with patch.object(
            douyin_commerce_service,
            "search_commerce_location_store_candidates",
            side_effect=search,
        ), patch.object(
            douyin_commerce_service,
            "_visible_store_listbox",
            side_effect=current_listbox,
        ), patch.object(
            douyin_commerce_service,
            "_apply_open_commerce_location_to_page",
            side_effect=apply_open,
            create=True,
        ), patch.object(
            douyin_commerce_service,
            "close_commerce_store_selector",
            new_callable=AsyncMock,
        ), patch.object(
            douyin_commerce_service,
            "_open_store_selector",
            new_callable=AsyncMock,
        ) as reopen:
            result = asyncio.run(
                douyin_commerce_service.apply_saved_commerce_location_to_page(
                    page, row, "domestic", [row["address"]]
                )
            )

        self.assertEqual(events, ["search", "listbox", "click"])
        self.assertEqual(result["matchedKeyword"], row["address"])
        reopen.assert_not_awaited()

    def test_saved_location_closes_first_search_before_keyword_fallback(self) -> None:
        """第一关键词未匹配时必须收口该面板，第二关键词才能使用新候选。"""

        page = object()
        listbox = object()
        target = {
            "poiId": "visible-poi:target",
            "name": "夜南香北京烤鸭",
            "address": "陕西省安康市汉滨区江北办富民街2号",
        }
        other = {
            "poiId": "visible-poi:other",
            "name": "其他地点",
            "address": "广西壮族自治区北海市海城区其他路1号",
        }
        events: list[str] = []

        async def search(_page, keyword, **_kwargs):
            events.append(f"search:{keyword}")
            return [dict(other)] if keyword == "店名" else [dict(target)]

        async def close(_page):
            events.append("close")

        async def apply_open(_page, actual_listbox, candidate):
            self.assertIs(actual_listbox, listbox)
            self.assertEqual(
                {key: candidate.get(key) for key in ("poiId", "name", "address")},
                target,
            )
            events.append("click")
            return {"location": dict(target)}

        with patch.object(
            douyin_commerce_service,
            "search_commerce_location_store_candidates",
            side_effect=search,
        ), patch.object(
            douyin_commerce_service,
            "close_commerce_store_selector",
            side_effect=close,
        ), patch.object(
            douyin_commerce_service,
            "_visible_store_listbox",
            new_callable=AsyncMock,
            return_value=listbox,
        ), patch.object(
            douyin_commerce_service,
            "_apply_open_commerce_location_to_page",
            side_effect=apply_open,
            create=True,
        ):
            result = asyncio.run(
                douyin_commerce_service.apply_saved_commerce_location_to_page(
                    page, target, "domestic", ["店名", target["address"]]
                )
            )

        first_search = events.index("search:店名")
        second_search = events.index(f"search:{target['address']}")
        self.assertIn("close", events[first_search + 1 : second_search])
        self.assertEqual(events[-2:], ["click", "close"])
        self.assertEqual(result["matchedKeyword"], target["address"])

    def test_saved_location_rejects_ambiguous_current_candidates(self) -> None:
        """当次面板出现两个同身份候选时必须安全停止，不得默认点第一条。"""

        target = {
            "poiId": "visible-poi:target",
            "name": "夜南香北京烤鸭",
            "address": "陕西省安康市汉滨区江北办富民街2号",
        }
        with patch.object(
            douyin_commerce_service,
            "search_commerce_location_store_candidates",
            new_callable=AsyncMock,
            return_value=[dict(target), dict(target)],
        ), patch.object(
            douyin_commerce_service,
            "close_commerce_store_selector",
            new_callable=AsyncMock,
        ):
            with self.assertRaisesRegex(
                douyin_commerce_service.DouyinCommerceError,
                "publish_location_candidate_ambiguous",
            ):
                asyncio.run(
                    douyin_commerce_service.apply_saved_commerce_location_to_page(
                        object(), target, "domestic", [target["address"]]
                    )
                )

    def test_saved_location_projects_selector_cleanup_failure(self) -> None:
        """旧面板无法收口时只允许固定错误码，不能继续搜索或泄露底层原文。"""

        with patch.object(
            douyin_commerce_service,
            "close_commerce_store_selector",
            new_callable=AsyncMock,
            side_effect=RuntimeError("sensitive-selector-detail"),
        ), patch.object(
            douyin_commerce_service,
            "search_commerce_location_store_candidates",
            new_callable=AsyncMock,
        ) as search:
            with self.assertRaisesRegex(
                douyin_commerce_service.DouyinCommerceError,
                "^publish_location_cleanup_incomplete$",
            ) as raised:
                asyncio.run(
                    douyin_commerce_service.apply_saved_commerce_location_to_page(
                        object(), self.location, "domestic", [self.location["address"]]
                    )
                )

        self.assertIsNone(raised.exception.__cause__)
        search.assert_not_awaited()

    def test_open_location_click_failure_uses_fixed_error_code(self) -> None:
        """唯一候选节点点击失败时不得把 Playwright 原文投影到任务。"""

        row = {
            "poiId": "visible-poi:target",
            "name": "夜南香北京烤鸭",
            "address": "陕西省安康市汉滨区江北办富民街2号",
        }
        target = MagicMock()
        target.scroll_into_view_if_needed = AsyncMock()
        target.click = AsyncMock(side_effect=RuntimeError("sensitive-dom-detail"))
        with patch.object(
            douyin_commerce_service,
            "_store_option_descriptors",
            new_callable=AsyncMock,
            return_value=[dict(row)],
        ), patch.object(
            douyin_commerce_service,
            "_location_option_targets",
            new_callable=AsyncMock,
            return_value=[target],
        ):
            with self.assertRaisesRegex(
                douyin_commerce_service.DouyinCommerceError,
                "^publish_location_click_failed$",
            ) as raised:
                asyncio.run(
                    douyin_commerce_service._apply_open_commerce_location_to_page(
                        object(), object(), row
                    )
                )

        self.assertIsNone(raised.exception.__cause__)

    def test_open_location_readback_mismatch_uses_fixed_error_code(self) -> None:
        """点击后带货模式或地点名不一致时不得返回应用成功。"""

        class Page:
            wait_for_timeout = AsyncMock()

        row = {
            "poiId": "visible-poi:target",
            "name": "夜南香北京烤鸭",
            "address": "陕西省安康市汉滨区江北办富民街2号",
        }
        target = MagicMock()
        target.scroll_into_view_if_needed = AsyncMock()
        target.click = AsyncMock()
        with patch.object(
            douyin_commerce_service,
            "_store_option_descriptors",
            new_callable=AsyncMock,
            return_value=[dict(row)],
        ), patch.object(
            douyin_commerce_service,
            "_location_option_targets",
            new_callable=AsyncMock,
            return_value=[target],
        ), patch.object(
            douyin_commerce_service,
            "_anchor_controls",
            new_callable=AsyncMock,
            return_value=(None, None, "带货模式", "其他地点"),
        ):
            with self.assertRaisesRegex(
                douyin_commerce_service.DouyinCommerceError,
                "^publish_location_readback_mismatch$",
            ):
                asyncio.run(
                    douyin_commerce_service._apply_open_commerce_location_to_page(
                        Page(), object(), row
                    )
                )


class DouyinCommerceLocationDomTests(unittest.IsolatedAsyncioTestCase):
    """用真实 DOM 约束地点 portal，避免把整张发布页的输入框算进来。"""

    async def test_anchor_controls_reads_selected_mode_without_open_menu_text(self) -> None:
        """模式菜单展开时只能回读已选值，不能把菜单全文当成当前模式。"""

        html = """
        <main>
          <section id="location-row">
            <span>位置</span>
            <div id="commerce-mode" class="semi-select" tabindex="0">
              <div class="semi-select-selection">
                <span class="semi-select-selection-text">带货模式</span>
              </div>
              <div role="listbox">
                <div role="option">带货模式</div>
                <div role="option">打卡模式</div>
              </div>
            </div>
            <input id="location-input">
          </section>
        </main>
        """
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(html)

                mode, store, mode_value, _ = (
                    await douyin_commerce_service._anchor_controls(page)
                )

                self.assertEqual(await mode.get_attribute("id"), "commerce-mode")
                self.assertEqual(await store.get_attribute("id"), "location-input")
                self.assertEqual(mode_value, "带货模式")
            finally:
                await browser.close()

    async def test_anchor_controls_pairs_css_position_input_with_adjacent_mode_row(self) -> None:
        """位置文案由 CSS 渲染时，不能把同一行的位置类型下拉当成带货模式。"""

        html = """
        <main>
          <div id="location-section">
            <section class="content-obt4oA new-layout-sLYOT6" style="display:flex;width:646px;height:32px">
              <div><span>添加标签</span></div>
              <div class="content-child-V0CB7w" style="display:flex;width:552px;height:32px">
                <div id="position-type" class="semi-select semi-select-single" style="width:104px;height:32px">
                  <div class="select-dropdown-option-video" data-code="poi"></div>
                </div>
                <div class="anchor-component-Shp3mT" style="width:440px;height:32px">
                  <div class="semi-select semi-select-single semi-select-filterable" style="width:440px;height:32px">
                    <input id="location-input">
                  </div>
                </div>
              </div>
            </section>
            <section class="content-obt4oA new-layout-sLYOT6" style="display:flex;width:646px;height:32px">
              <div style="width:74px;height:32px"></div>
              <div id="commerce-mode" class="semi-select semi-select-single semi-select-filterable" style="width:552px;height:32px">
                <div class="semi-select-selection"><span class="semi-select-selection-placeholder"></span></div>
              </div>
            </section>
            <section class="content-obt4oA new-layout-sLYOT6" style="display:flex;width:646px;height:32px">
              <div style="width:74px;height:32px"></div>
              <div style="display:flex;width:552px;height:32px">
                <div class="semi-select semi-select-single" style="width:128px;height:32px"></div>
                <div class="semi-select semi-select-multiple semi-select-filterable" style="width:412px;height:32px">
                  <input style="width:2px">
                </div>
              </div>
            </section>
          </div>
        </main>
        """
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(html)

                mode, store, _, store_text = (
                    await douyin_commerce_service._anchor_controls(page)
                )

                self.assertEqual(await mode.get_attribute("id"), "commerce-mode")
                self.assertEqual(await store.get_attribute("id"), "location-input")
                self.assertEqual(store_text, "")
            finally:
                await browser.close()

    async def test_blank_adjacent_select_does_not_override_direct_location_input(self) -> None:
        """存在原生地点输入时，相邻空下拉不再按旧版模式菜单处理。"""

        html = """
        <main>
          <div id="location-section">
            <section class="content-obt4oA new-layout-sLYOT6" style="display:flex;width:646px;height:32px">
              <div><span>添加标签</span></div>
              <div class="content-child-V0CB7w" style="display:flex;width:552px;height:32px">
                <div id="position-type" class="semi-select semi-select-single" style="width:104px;height:32px"></div>
                <div style="width:440px;height:32px">
                  <div class="semi-select semi-select-single semi-select-filterable" style="width:440px;height:32px">
                    <input id="location-input">
                  </div>
                </div>
              </div>
            </section>
            <section class="content-obt4oA new-layout-sLYOT6" style="display:flex;width:646px;height:32px">
              <div style="width:74px;height:32px"></div>
              <div id="commerce-mode" class="semi-select semi-select-single semi-select-filterable" style="width:552px;height:32px">
                <div id="mode-selection" class="semi-select-selection" style="width:506px;height:30px">
                  <span id="mode-value" class="semi-select-selection-placeholder"></span>
                </div>
              </div>
            </section>
          </div>
          <div id="mode-list" role="listbox" style="display:none;width:200px;height:80px">
            <div id="commerce-option" role="option" style="height:32px">带货模式</div>
            <div role="option" style="height:32px">打卡模式</div>
          </div>
          <script>
            document.querySelector('#mode-selection').addEventListener('click', () => {
              document.querySelector('#mode-list').style.display = 'block';
            });
            document.querySelector('#commerce-option').addEventListener('click', () => {
              const value = document.querySelector('#mode-value');
              value.textContent = '带货模式';
              value.className = 'semi-select-selection-text';
              document.querySelector('#mode-list').style.display = 'none';
            });
          </script>
        </main>
        """
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(html)

                store = await douyin_commerce_service._ensure_local_group_buy_mode(page)

                self.assertEqual(await store.get_attribute("id"), "location-input")
                self.assertEqual(
                    await page.locator("#mode-value").inner_text(),
                    "",
                )
                self.assertEqual(
                    await page.locator("#mode-list").evaluate(
                        "node => getComputedStyle(node).display"
                    ),
                    "none",
                )
            finally:
                await browser.close()

    async def test_direct_location_input_bypasses_unrelated_empty_adjacent_select(self) -> None:
        """新版已有原生地点输入时，不得再打开相邻的其他筛选下拉。"""

        mode_control = object()
        location_input = object()
        with patch.object(
            douyin_commerce_service,
            "_anchor_controls",
            new_callable=AsyncMock,
            return_value=(
                mode_control,
                location_input,
                douyin_commerce_service._UNSELECTED_MODE_VALUE,
                "",
            ),
        ), patch.object(
            douyin_commerce_service,
            "_is_direct_location_entry",
            new_callable=AsyncMock,
            return_value=True,
        ), patch.object(
            douyin_commerce_service,
            "_open_exact_select",
            new_callable=AsyncMock,
        ) as open_select, patch.object(
            douyin_commerce_service,
            "_wait_mode_options",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await douyin_commerce_service._ensure_local_group_buy_mode(object())

        self.assertIs(result, location_input)
        open_select.assert_not_awaited()

    async def test_nonempty_mode_placeholder_is_treated_as_unselected(self) -> None:
        """抖音新版会给空模式渲染提示文案，不能把提示文案当成未知模式。"""

        html = """
        <main>
          <div id="location-section">
            <section class="content-obt4oA new-layout-sLYOT6" style="display:flex;width:646px;height:32px">
              <div><span>添加标签</span></div>
              <div class="content-child-V0CB7w" style="display:flex;width:552px;height:32px">
                <div class="semi-select semi-select-single" style="width:104px;height:32px"></div>
                <div style="width:440px;height:32px">
                  <div class="semi-select semi-select-single semi-select-filterable" style="width:440px;height:32px">
                    <input id="location-input">
                  </div>
                </div>
              </div>
            </section>
            <section class="content-obt4oA new-layout-sLYOT6" style="display:flex;width:646px;height:32px">
              <div style="width:74px;height:32px"></div>
              <div id="commerce-mode" class="semi-select semi-select-single semi-select-filterable" style="width:552px;height:32px">
                <div class="semi-select-selection">
                  <span class="semi-select-selection-text semi-select-selection-placeholder">请选择带货模式</span>
                </div>
              </div>
            </section>
          </div>
        </main>
        """
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(html)

                _, _, mode_value, _ = await douyin_commerce_service._anchor_controls(page)

                self.assertEqual(
                    mode_value,
                    douyin_commerce_service._UNSELECTED_MODE_VALUE,
                )
            finally:
                await browser.close()

    async def test_add_tag_opens_unique_focusable_ancestor_from_exact_text_leaf(self) -> None:
        html = """
        <main>
          <div id="add-tag" tabindex="0"><span>添加标签</span></div>
          <script>
            document.querySelector('#add-tag').addEventListener('click', () => {
              document.body.dataset.addTagOpened = 'yes';
            });
          </script>
        </main>
        """
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(html)

                opened = await douyin_commerce_service._open_unique_add_tag(page)

                self.assertTrue(opened)
                self.assertEqual(
                    await page.locator("body").get_attribute("data-add-tag-opened"),
                    "yes",
                )
            finally:
                await browser.close()

    async def test_add_tag_opens_unique_select_in_following_sibling_area(self) -> None:
        """旧版标题是静态文字，真正入口位于同一行右侧兄弟区域。"""

        html = """
        <main>
          <section class="anchor-item">
            <div class="anchor-item-label"><span>添加标签</span></div>
            <div class="anchor-item-content">
              <div id="add-tag-select" class="semi-select-single" tabindex="0">
                请选择
              </div>
            </div>
          </section>
          <script>
            document.querySelector('#add-tag-select').addEventListener('click', () => {
              document.body.dataset.addTagOpened = 'yes';
            });
          </script>
        </main>
        """
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(html)

                opened = await douyin_commerce_service._open_unique_add_tag(page)

                self.assertTrue(opened)
                self.assertEqual(
                    await page.locator("body").get_attribute("data-add-tag-opened"),
                    "yes",
                )
            finally:
                await browser.close()

    async def test_add_tag_opens_unique_modern_content_child_after_music_reset(self) -> None:
        """选音乐重渲染后，入口是同一行右侧无 role/tabindex 的唯一容器。"""

        html = """
        <main>
          <section class="content-obt4oA new-layout-sLYOT6" style="display:flex;width:646px;height:32px">
            <div>
              <div class="title-dS7kae"><span class="title-content-oaqcSp">添加标签</span></div>
            </div>
            <div id="modern-add-tag" class="content-child-V0CB7w content-limit-width-zybqBW" style="width:552px;height:32px"></div>
          </section>
          <script>
            document.querySelector('#modern-add-tag').addEventListener('click', () => {
              document.body.dataset.addTagOpened = 'yes';
            });
          </script>
        </main>
        """
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(html)

                opened = await douyin_commerce_service._open_unique_add_tag(page)

                self.assertTrue(opened)
                self.assertEqual(
                    await page.locator("body").get_attribute("data-add-tag-opened"),
                    "yes",
                )
            finally:
                await browser.close()

    async def test_modern_add_tag_ignores_unrelated_focusable_controls_below_row(self) -> None:
        """新版精确行存在时，不得被后续声明区等可聚焦控件干扰。"""

        html = """
        <main>
          <section class="container-EMGgQp">
            <section class="content-obt4oA new-layout-sLYOT6" style="display:flex;width:646px;height:32px">
              <div>
                <div class="title-dS7kae"><span class="title-content-oaqcSp">添加标签</span></div>
              </div>
              <div id="modern-add-tag" class="content-child-V0CB7w content-limit-width-zybqBW" style="width:552px;height:32px"></div>
            </section>
          </section>
          <section class="container-EMGgQp">
            <button id="unrelated">后续声明区按钮</button>
          </section>
          <script>
            document.querySelector('#modern-add-tag').addEventListener('click', () => {
              document.body.dataset.addTagOpened = 'yes';
            });
          </script>
        </main>
        """
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(html)

                opened = await douyin_commerce_service._open_unique_add_tag(page)

                self.assertTrue(opened)
                self.assertEqual(
                    await page.locator("body").get_attribute("data-add-tag-opened"),
                    "yes",
                )
            finally:
                await browser.close()

    async def test_add_tag_rejects_multiple_modern_content_children(self) -> None:
        """右侧候选不唯一时仍应安全停止，不能按位置猜测。"""

        html = """
        <main>
          <section class="content-obt4oA new-layout-sLYOT6" style="display:flex;width:646px;height:32px">
            <div><div class="title-dS7kae"><span>添加标签</span></div></div>
            <div class="content-child-first" style="width:276px;height:32px"></div>
            <div class="content-child-second" style="width:276px;height:32px"></div>
          </section>
        </main>
        """
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(html)

                opened = await douyin_commerce_service._open_unique_add_tag(page)

                self.assertFalse(opened)
            finally:
                await browser.close()

    async def test_add_tag_rejects_ambiguous_following_sibling_selects(self) -> None:
        html = """
        <main>
          <section>
            <div><span>添加标签</span></div>
            <div>
              <div class="semi-select-single" tabindex="0">请选择 A</div>
              <div class="semi-select-single" tabindex="0">请选择 B</div>
            </div>
          </section>
        </main>
        """
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(html)

                opened = await douyin_commerce_service._open_unique_add_tag(page)

                self.assertFalse(opened)
                self.assertEqual(
                    await page.locator(
                        '[data-oneclick-commerce-add-tag="active"]'
                    ).count(),
                    0,
                )
            finally:
                await browser.close()

    async def test_location_portal_ignores_eight_other_editor_fields(self) -> None:
        html = """
        <main id="editor-root">
          <input id="title"><textarea id="description"></textarea>
          <input id="tag"><input id="schedule-date"><input id="schedule-time">
          <input id="collaboration"><input id="cover"><input id="other">
          <div data-oneclick-commerce-store="active">输入地理位置</div>
          <section id="location-portal">
            <nav><button>本地</button><button>国内</button></nav>
            <input id="location-input">
          </section>
        </main>
        """
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(html)

                locator = await douyin_commerce_service._visible_commerce_search_input(page)

                self.assertEqual(await locator.get_attribute("id"), "location-input")
                self.assertEqual(
                    await page.locator(
                        '[data-oneclick-commerce-search-input="active"]'
                    ).count(),
                    1,
                )
            finally:
                await browser.close()

    async def test_location_portal_rejects_two_editable_fields(self) -> None:
        html = """
        <main id="editor-root">
          <div data-oneclick-commerce-store="active">输入地理位置</div>
          <section id="location-portal">
            <nav><button>本地</button><button>国内</button></nav>
            <input id="location-input"><input id="ambiguous-input">
          </section>
        </main>
        """
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(html)

                with self.assertRaisesRegex(
                    douyin_commerce_service.DouyinCommerceError,
                    "实际 2 个",
                ):
                    await douyin_commerce_service._visible_commerce_search_input(page)
            finally:
                await browser.close()

    async def test_local_location_list_keeps_complete_results_beside_helper_option(self) -> None:
        """本地列表含无地址辅助项时，仍应读取同面板内的完整官方地点。"""

        html = """
        <main id="editor-root">
          <div data-oneclick-commerce-store="active">输入地理位置</div>
          <section id="location-portal">
            <nav><button>本地</button><button>国内</button></nav>
            <input id="location-input" value="夜南香">
            <div id="local-results" role="listbox">
              <div role="option">不展示地理位置</div>
              <div role="option">
                <div class="name-local">夜南香北京烤鸭团购信息</div>
                <div>团购专区</div>
                <div>15 件商品 · 15 件返佣</div>
              </div>
              <div role="option">
                <div class="name-local">夜南香北京烤鸭（万泉城店）</div>
                <div class="address-local">广西壮族自治区北海市银海区银滩大道 1 号</div>
              </div>
            </div>
          </section>
        </main>
        """
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(html)
                await douyin_commerce_service._visible_commerce_search_input(page)

                listbox = await douyin_commerce_service._visible_store_listbox(page)

                self.assertIsNotNone(listbox)
                self.assertEqual(await listbox.get_attribute("id"), "local-results")
                rows = await douyin_commerce_service._store_option_descriptors(listbox)
                candidates = douyin_commerce_service.normalize_commerce_location_candidates(
                    rows
                )
                self.assertEqual(len(candidates), 1)
                self.assertEqual(
                    candidates[0]["name"],
                    "夜南香北京烤鸭（万泉城店）",
                )
                self.assertEqual(
                    candidates[0]["address"],
                    "广西壮族自治区北海市银海区银滩大道 1 号",
                )
            finally:
                await browser.close()

    async def test_commerce_summary_with_district_word_is_not_a_complete_address(self) -> None:
        """“团购专区”含“区”字也不能冒充平台返回的完整地址。"""

        html = """
        <main id="editor-root">
          <input data-oneclick-commerce-store="active" id="location-input">
          <section id="location-portal">
            <nav><button>本地</button><button>国内</button></nav>
            <div id="commerce-only-results" role="listbox">
              <div role="option">
                <div class="name-local">夜南香北京烤鸭</div>
                <div>团购专区</div>
                <div>15 件商品 · 15 件返佣</div>
              </div>
            </div>
          </section>
        </main>
        """
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(html)

                listbox = await douyin_commerce_service._visible_store_listbox(page)

                self.assertIsNone(listbox)
            finally:
                await browser.close()

    async def test_close_location_selector_detects_helper_only_result_panel(self) -> None:
        """空结果辅助列表也必须按 Escape 收口，不能残留到下一次搜索。"""

        html = """
        <main id="editor-root">
          <section id="location-row">
            <span>位置</span>
            <div class="semi-select" tabindex="0">
              <span class="semi-select-selection-text">带货模式</span>
              <div id="mode-results" role="listbox">
                <div role="option">带货模式</div>
                <div role="option">打卡模式</div>
              </div>
            </div>
            <section id="location-portal">
              <nav><button>本地</button><button>国内</button></nav>
              <input data-oneclick-commerce-store="active" id="location-input">
              <div id="helper-results" role="listbox">
                <div role="option">未找到相关地点</div>
              </div>
            </section>
          </section>
          <script>
            document.querySelector('#location-input').addEventListener('keydown', event => {
              if (event.key === 'Escape') {
                document.querySelector('#helper-results').style.display = 'none';
              }
            });
          </script>
        </main>
        """
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(html)

                await douyin_commerce_service.close_commerce_store_selector(page)

                self.assertFalse(await page.locator("#helper-results").is_visible())
            finally:
                await browser.close()

    async def test_close_location_selector_uses_escape_for_direct_input_results(self) -> None:
        """新版输入框的完整候选列表不能靠再次点击输入框关闭。"""

        html = """
        <main id="editor-root">
          <section id="location-row">
            <span>位置</span>
            <div class="semi-select" tabindex="0">
              <span class="semi-select-selection-text">带货模式</span>
            </div>
            <section id="location-portal">
              <nav><button>本地</button><button>国内</button></nav>
              <input data-oneclick-commerce-store="active" id="location-input">
              <div id="complete-results" role="listbox">
                <div role="option">
                  <div class="name-local">夜南香北京烤鸭（万泉城店）</div>
                  <div class="address-local">广西壮族自治区北海市银海区银滩大道 1 号</div>
                </div>
              </div>
            </section>
          </section>
          <script>
            document.querySelector('#location-input').addEventListener('keydown', event => {
              if (event.key === 'Escape') {
                document.querySelector('#complete-results').style.display = 'none';
              }
            });
          </script>
        </main>
        """
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(html)

                await douyin_commerce_service.close_commerce_store_selector(page)

                self.assertFalse(await page.locator("#complete-results").is_visible())
            finally:
                await browser.close()


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

    def test_music_cache_uses_metadata_fingerprint_when_platform_id_is_missing(self) -> None:
        """新版收藏页未暴露 ID 时，缓存仍可按完整公开元数据稳定复用。"""

        visible = {
            "musicId": "visible:测试歌|测试作者|01:08|favorite-index:1",
            "title": "测试歌",
            "creator": "测试作者",
            "duration": "01:08",
            "marker": "favorite-0",
        }
        cached = douyin_favorite_music_cache._normalize_cache_row(visible)

        self.assertIsNotNone(cached)
        self.assertTrue(cached["musicId"].startswith("metadata:"))
        matched = douyin_music_service.find_favorite_music_by_id([visible], cached["musicId"])
        self.assertEqual(matched["musicId"], visible["musicId"])

    def test_music_metadata_fingerprint_is_persisted_and_reloaded(self) -> None:
        """无平台 ID 的收藏音乐必须真实写入 SQLite，并可被下一次批量加载。"""

        visible = {
            "musicId": "visible:测试歌|测试作者|01:08|favorite-index:1",
            "title": "测试歌",
            "creator": "测试作者",
            "duration": "01:08",
        }
        with tempfile.TemporaryDirectory() as root:
            temporary_database = Path(root) / "cache.db"
            with patch.object(database, "DB_PATH", temporary_database):
                saved = douyin_favorite_music_cache.replace_cached_favorite_music(801, [visible])
                loaded = douyin_favorite_music_cache.list_cached_favorite_music(801)

        self.assertEqual(saved, loaded)
        self.assertEqual(loaded[0]["title"], "测试歌")
        self.assertTrue(loaded[0]["musicId"].startswith("metadata:"))

    def test_music_metadata_cache_rejects_ambiguous_current_candidates(self) -> None:
        """同名、同作者、同时长的多个当前候选不能猜测选择。"""

        visible = {
            "musicId": "visible:测试歌|测试作者|01:08|favorite-index:1",
            "title": "测试歌",
            "creator": "测试作者",
            "duration": "01:08",
        }
        cached = douyin_favorite_music_cache._normalize_cache_row(visible)
        with self.assertRaisesRegex(douyin_music_service.DouyinMusicError, "不唯一"):
            douyin_music_service.find_favorite_music_by_id(
                [visible, dict(visible, musicId="visible:测试歌|测试作者|01:08|favorite-index:2")],
                cached["musicId"],
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

    async def test_cached_metadata_music_keeps_cache_identity_after_current_picker_readback(self) -> None:
        """批量执行器比较缓存身份时，临时列表身份不能覆盖它。"""

        class OpenPage:
            def is_closed(self) -> bool:
                return False

        current = {
            "musicId": "visible:测试歌|测试作者|01:08|favorite-index:1",
            "title": "测试歌",
            "creator": "测试作者",
            "duration": "01:08",
            "marker": "favorite-0",
        }
        cached = douyin_favorite_music_cache._normalize_cache_row(current)
        manager = douyin_commerce_session.DouyinCommerceSessionManager()
        manager._session = douyin_commerce_session._CommerceEditorSession(
            session_id="metadata-session",
            upload_payload={},
            account_name="测试账号",
            browser=None,
            context=None,
            page=OpenPage(),
            playwright=None,
            uploader=None,
            music_dialog=object(),
            music_candidates=[current],
        )
        with patch.object(
            manager,
            "_select_favorite_music",
            new=AsyncMock(return_value=dict(current)),
        ) as select:
            selected = await manager._select_cached_favorite_music(
                "metadata-session", cached["musicId"]
            )

        select.assert_awaited_once_with("metadata-session", current["musicId"])
        self.assertEqual(selected["musicId"], cached["musicId"])


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

    def test_copy_collector_diagnostics_is_non_blocking_and_whitelisted(self) -> None:
        """主 UI 专项也必须覆盖诊断复制入口与非阻塞约束。"""

        self.page._setup_generation_id = "8f31c9ab-generation"
        event = {
            "timestamp": "2026-08-09T13:42:10+08:00",
            "requestId": "request-a",
            "setupGenerationId": "8f31c9ab-generation",
            "collectorType": "local_location",
            "collectorInstanceId": "local-a",
            "accountMaskedId": "account-31",
            "phase": "result",
            "action": "search_locations",
            "scope": "local",
            "keyword": "夜南香",
            "attempt": 1,
            "candidateCount": 0,
            "durationMs": 1200,
            "outcome": "failed",
            "errorCode": "candidate_panel_missing",
            "cleanupResult": "closed",
            "sessionId": "session-secret",
            "rawDetail": "cookiesFile/account.json Cookie=secret DOM=<html> 123456",
        }
        success_event = {
            **event,
            "timestamp": "2026-08-09T13:42:11+08:00",
            "requestId": "request-b",
            "collectorType": "favorite_music",
            "collectorInstanceId": "music-a",
            "action": "refresh_favorite_music",
            "keyword": "",
            "candidateCount": 3,
            "durationMs": 37,
            "outcome": "success",
            "errorCode": "collector_unknown",
            "cleanupResult": "",
        }

        with patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.recent_diagnostics",
            return_value=[event, success_event],
        ), patch(
            "ui.douyin_commerce_page.QApplication.clipboard"
        ) as clipboard, patch(
            "ui.douyin_commerce_page.QTimer.singleShot"
        ), patch(
            "ui.douyin_commerce_page.QMessageBox.information"
        ) as information, patch(
            "ui.douyin_commerce_page.QMessageBox.warning"
        ) as warning, patch(
            "ui.douyin_commerce_page.QMessageBox.critical"
        ) as critical:
            self.page._copy_collector_diagnostics()

        copied = clipboard.return_value.setText.call_args.args[0]
        self.assertIn(
            "13:42:10 | 批次 8f31c9 | 本地点 | search_locations", copied
        )
        self.assertIn("关键词=夜南香 | 候选=0 | 1200ms", copied)
        self.assertIn("candidate_panel_missing | cleanup=closed", copied)
        self.assertIn("收藏音乐 | refresh_favorite_music", copied)
        self.assertIn("候选=3 | 37ms | ok | cleanup=-", copied)
        for secret in (
            "session-secret",
            "cookiesFile/account.json",
            "Cookie",
            "DOM",
            "<html>",
            "123456",
        ):
            self.assertNotIn(secret, copied)
        information.assert_not_called()
        warning.assert_not_called()
        critical.assert_not_called()

    def test_copy_collector_diagnostics_uses_fixed_empty_copy(self) -> None:
        self.page._setup_generation_id = "generation-empty"
        with patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.recent_diagnostics",
            return_value=[],
        ), patch(
            "ui.douyin_commerce_page.QApplication.clipboard"
        ) as clipboard, patch(
            "ui.douyin_commerce_page.QMessageBox.information"
        ) as information:
            self.page._copy_collector_diagnostics()

        clipboard.return_value.setText.assert_called_once_with(
            "当前批次暂无采集诊断"
        )
        information.assert_not_called()

    def test_copy_collector_diagnostics_redacts_reviewer_payloads(self) -> None:
        self.page._setup_generation_id = "generation-redaction"
        payloads = (
            "/Users/andy/private/account.json",
            "/tmp/browser-profile/state.json",
            'selector=<div data-secret="abc">private</div>',
            "access_token=top-secret",
            "cookie_value=top-secret",
        )
        events = [
            {
                "timestamp": f"2026-08-09T13:42:1{index}+08:00",
                "requestId": f"request-{index}",
                "setupGenerationId": "generation-redaction",
                "collectorType": "domestic_location",
                "collectorInstanceId": f"domestic-{index}",
                "accountMaskedId": "account-31",
                "phase": "result",
                "action": "search_locations",
                "scope": "domestic",
                "keyword": payload,
                "attempt": 1,
                "candidateCount": 0,
                "durationMs": 10,
                "outcome": "failed",
                "errorCode": "collector_unknown",
                "cleanupResult": "",
            }
            for index, payload in enumerate(payloads)
        ]

        with patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.recent_diagnostics",
            return_value=events,
        ), patch(
            "ui.douyin_commerce_page.QApplication.clipboard"
        ) as clipboard:
            self.page._copy_collector_diagnostics()

        copied = clipboard.return_value.setText.call_args.args[0].casefold()
        for forbidden in (
            "/users",
            "andy",
            "private",
            "account.json",
            "/tmp",
            "browser-profile",
            "state.json",
            "selector",
            "<div",
            "data-secret",
            "access_token",
            "cookie_value",
            "top-secret",
        ):
            self.assertNotIn(forbidden, copied)

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

    def test_batch_progress_shows_completed_success_and_failure_counts(self) -> None:
        """逐条发布期间底部必须实时展示累计结果，而非只显示当前序号。"""

        self.page._batch_task_id = 601
        with patch(
            "ui.douyin_commerce_page.task_service.get_task",
            return_value={
                "items": [
                    {"status": "success"},
                    {"status": "failed"},
                    {"status": "running"},
                    {"status": "pending"},
                ]
            },
        ):
            self.page._batch_progress(
                {"index": 2, "total": 4, "phase": "uploading", "message": "正在上传视频"}
            )

        self.assertEqual(
            self.page.validation_label.text(),
            "已完成 2/4 · 成功 1 · 失败 1 · 正在处理 3/4：正在上传视频",
        )

    def test_ambiguous_platform_receipt_summary_reports_paused_and_pending_review(self) -> None:
        """最终提交回执不明时，弹窗不得误报整批已结束或漏计当前视频。"""

        result = [
            {
                "index": 0,
                "status": "receipt_ambiguous",
                "diagnostic": "定时提交后未能从作品管理页回读标题和指定时间",
            },
            *[
                {"index": index, "status": "pending"}
                for index in range(1, 19)
            ],
        ]

        with patch("ui.douyin_commerce_page.QMessageBox.warning"), patch(
            "ui.douyin_commerce_page.QMessageBox.information"
        ):
            self.page._batch_publish_succeeded(result)

        self.assertEqual(
            self.page.validation_label.text(),
            "批量提交已暂停：成功 0 条，失败 0 条；"
            "平台状态待核对 1 条；未开始 18 条；"
            "待核对原因：定时提交后未能从作品管理页回读标题和指定时间。",
        )

    def test_content_layout_moves_video_tags_and_local_content_to_requested_areas(self) -> None:
        """内容页必须以账号含视频、内容含标签历史和本地内容、右侧日志呈现。"""

        self.assertEqual(
            self.page.video_preview.parentWidget().objectName(),
            "douyinCommerceContentAccountColumn",
        )
        self.assertEqual(
            self.page.tag_history_host.parentWidget().objectName(),
            "douyinCommerceContentBodyColumn",
        )
        self.assertEqual(
            self.page.content_save_status.parentWidget().objectName(),
            "douyinCommerceContentBodyColumn",
        )
        self.assertEqual(
            self.page.content_execution_log.objectName(),
            "douyinCommerceExecutionLog",
        )
        self.assertEqual(
            self.page.platform_execution_log.objectName(),
            "douyinCommerceExecutionLog",
        )
        self.assertEqual(
            self.page.review_execution_log.objectName(),
            "douyinCommerceExecutionLog",
        )

    def test_tag_history_chip_exposes_a_local_remove_button(self) -> None:
        """最近标签的删除按钮只删除本地历史，不修改当前已选标签。"""

        self.page._tag_history = ["北海", "探店"]
        self.page._render_tag_history()
        remove_button = next(
            button
            for button in reversed(
                self.page.tag_history_host.findChildren(
                    QPushButton, "douyinCommerceTagHistoryRemove"
                )
            )
            if button.toolTip() == "删除历史标签 #北海"
        )
        self.assertIsNotNone(remove_button)
        with patch(
            "ui.douyin_commerce_page.douyin_commerce_draft_service.remove_tag_history",
            return_value=["探店"],
        ) as remove_history:
            remove_button.click()

        remove_history.assert_called_once_with("北海")
        self.assertEqual(self.page._tag_history, ["探店"])

    def test_execution_log_panel_receives_runtime_output(self) -> None:
        """三个日志面板均订阅同一运行日志总线。"""

        marker = "日志面板回归标识"
        runtime_log_bus().publish(marker)
        self.app.processEvents()

        self.assertIn(marker, self.page.content_execution_log.output.toPlainText())
        self.assertIn(marker, self.page.platform_execution_log.output.toPlainText())
        self.assertIn(marker, self.page.review_execution_log.output.toPlainText())

    def test_execution_log_panel_copies_visible_log_text(self) -> None:
        """复制按钮应将当前执行日志完整写入系统剪贴板。"""

        runtime_log_bus().publish("复制日志回归标识")
        self.app.processEvents()
        with patch("ui.runtime_log.QApplication.clipboard") as clipboard:
            self.page.content_execution_log.copy_execution_log()

        copied = clipboard.return_value.setText.call_args.args[0]
        self.assertIn("复制日志回归标识", copied)
        self.assertIn("已复制", self.page.content_execution_log.copy_button.text())

    def test_execution_log_panel_clears_shared_log_history(self) -> None:
        """清空日志应同时清除所有步骤共享的运行记录。"""

        runtime_log_bus().publish("清空日志回归标识")
        self.app.processEvents()
        self.page.content_execution_log.clear_execution_log()

        self.assertEqual(runtime_log_bus().history(), [])
        self.assertEqual(self.page.content_execution_log.output.toPlainText(), "")
        self.assertEqual(self.page.review_execution_log.output.toPlainText(), "")
        self.assertIn("已清空", self.page.content_execution_log.clear_button.text())

    def test_platform_settings_prioritize_two_work_columns_over_logs(self) -> None:
        """平台设置只保留共享设置与逐条地点两栏，日志不占主工作区。"""

        self.assertEqual(self.page.content_columns.columnStretch(2), 27)
        self.assertEqual(self.page.content_columns.columnMinimumWidth(2), 300)
        self.assertEqual(self.page.platform_columns.columnStretch(0), 3)
        self.assertEqual(self.page.platform_columns.columnStretch(1), 7)
        self.assertEqual(self.page.platform_columns.columnStretch(2), 0)
        self.assertTrue(self.page.platform_execution_log.isHidden())
        self.assertEqual(self.page.review_columns.columnStretch(0), 25)
        self.assertEqual(self.page.review_columns.columnStretch(1), 50)
        self.assertEqual(self.page.review_columns.columnStretch(2), 25)
        self.assertEqual(self.page.review_columns.columnMinimumWidth(2), 300)
        self.assertIs(
            self.page.review_submission_panel.parentWidget(),
            self.page.review_execution_log.parentWidget(),
        )

    def test_declaration_summary_card_is_not_rendered_in_platform_settings(self) -> None:
        """声明单选项已足够表达状态，不应再占用额外的长摘要框。"""

        declaration_layout = self.page.declaration_panel.layout()
        self.assertIsNotNone(declaration_layout)
        self.assertEqual(declaration_layout.indexOf(self.page.declaration_card), -1)

    def test_review_log_is_a_sibling_module_not_part_of_review_panel(self) -> None:
        """检查页日志必须是独立模块，不能嵌套在检查提交卡中。"""

        self.assertIs(self.page.review_panel.parentWidget(), self.page.review_execution_log.parentWidget())
        self.assertFalse(self.page.review_panel.isAncestorOf(self.page.review_execution_log))
        self.assertEqual(self.page.review_execution_log.objectName(), "douyinCommerceExecutionLog")

    def test_review_summary_formats_account_subject_and_video_metadata(self) -> None:
        """检查摘要需提供账号主体及本机视频的完整基础信息。"""

        account = {"userName": "逆浪风", "profileName": "3199"}
        video = {"filename": "doubao_video_9.mp4", "storedPath": "C:/demo.mp4"}
        with patch.object(self.page, "_selected_account", return_value=account), patch.object(
            self.page, "_selected_video", return_value=video
        ), patch.object(
            media_service,
            "video_display_metadata",
            return_value={"durationText": "00:10", "resolution": "1280 × 720"},
        ):
            summary = self.page._summary()

        self.assertEqual(summary["account"], "逆浪风\n主体：3199")
        self.assertEqual(summary["video"], "doubao_video_9.mp4\n00:10 · 1280 × 720")

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
        self.page._session_id = "session-demo"
        self.page._sync_view()
        self.assertFalse(self.page.declaration_buttons["内容为转载信息"].isEnabled())
        self.assertTrue(self.page.declaration_buttons["内容由AI生成"].isEnabled())
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

    def test_upload_payload_includes_selected_builtin_account_id(self) -> None:
        """平台设置探针源载荷必须携带当前账号的本机整数 ID。"""

        with tempfile.TemporaryDirectory() as root:
            video = Path(root) / "commerce.mp4"
            video.write_bytes(b"video")
            account = {
                "id": 73,
                "type": 3,
                "status": 1,
                "filePath": "oneclick_3_demo.json",
            }
            media = {"storedPath": str(video), "filename": "commerce.mp4"}
            self.page.title_input.setText("抖音带货测试")
            self.page.description_input.setPlainText("仅验证账号 ID 载荷。")
            with patch.object(
                self.page, "_selected_account", return_value=account
            ), patch.object(self.page, "_selected_video", return_value=media):
                payload = self.page.collect_upload_payload()

        self.assertIs(type(payload["accountId"]), int)
        self.assertEqual(payload["accountId"], 73)

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

    def test_music_candidates_expand_inside_music_card_without_overlaying_declaration(self) -> None:
        """收藏音乐候选必须使用卡片内滚动列表，而不是覆盖声明区的原生浮层。"""

        candidates = [
            {"musicId": f"music-{index}", "title": f"音乐 {index}", "creator": "作者"}
            for index in range(8)
        ]
        self.page._show_music_candidates(candidates)
        self.page.music_combo.showPopup()

        self.assertFalse(self.page.music_candidate_list.isHidden())
        self.assertEqual(self.page.music_candidate_list.count(), 8)
        self.assertEqual(self.page.music_candidate_list.maximumHeight(), 184)
        self.assertIs(self.page.music_candidate_list.parentWidget(), self.page.music_panel)
        self.assertFalse(self.page.music_combo.view().isVisible())

    def test_first_music_dropdown_click_only_uses_local_cache(self) -> None:
        """首次点击没有缓存时不访问平台，用户必须明确点刷新。"""

        self.page._session_id = "session-demo"
        with patch.object(
            self.page, "_refresh_favorite_music_candidates"
        ) as refresh:
            self.page.music_combo.showPopup()
        refresh.assert_not_called()
        self.assertIn("点击刷新", self.page.music_status.text())

    def test_refresh_music_returns_closed_picker_candidates_as_cache(self) -> None:
        """刷新只同步候选；不能让音乐抽屉阻塞后续地点设置。"""

        self.page._session_id = "session-demo"
        with patch.object(
            self.page, "_start_immediate_write", return_value=True
        ) as start:
            self.page._refresh_favorite_music_candidates()
        self.assertEqual(start.call_args.args[0], "music_refresh")

        candidate = {
            "musicId": "music-001",
            "title": "出埃及记",
            "creator": "石Yuchi",
            "duration": "01:08",
        }
        on_success = start.call_args.args[2]
        with patch.object(self.page, "_show_music_candidates") as show:
            on_success([candidate])
        show.assert_called_once_with([candidate], source="cache")

    def test_cached_music_selection_uses_exact_current_editor_recheck(self) -> None:
        """缓存候选点击后必须走受控重识别入口，不能直接复用本地数据。"""

        self.page._session_id = "session-demo"
        candidate = {
            "musicId": "music-001",
            "title": "出埃及记",
            "creator": "石Yuchi",
            "duration": "01:08",
        }
        self.page._show_music_candidates([candidate], source="cache")
        with patch.object(self.page.runner, "is_running", return_value=False), patch.object(
            self.page.runner, "run", return_value=True
        ) as run:
            self.page._start_music_write(candidate)

        work = run.call_args.args[1]
        with patch.object(
            douyin_commerce_session.commerce_session_manager,
            "select_cached_favorite_music",
            return_value=candidate,
        ) as select_cached:
            self.assertEqual(work(), candidate)
        select_cached.assert_called_once_with("session-demo", "music-001")

    def test_cached_music_results_are_displayed_without_platform_refresh(self) -> None:
        self.page._show_music_candidates(
            [
                {
                    "musicId": "music-cache-001",
                    "title": "本地缓存歌曲",
                    "creator": "收藏作者",
                    "duration": "03:21",
                }
            ],
            source="cache",
        )
        self.assertEqual(self.page._music_candidate_source, "cache")
        self.assertEqual(self.page.music_combo.count(), 2)
        self.assertIn("本地缓存歌曲", self.page.music_combo.itemText(1))

    def test_music_refresh_completion_reopens_candidate_list(self) -> None:
        self.page._music_candidates = [
            {"musicId": "music-001", "title": "出埃及记"}
        ]
        self.page._immediate_write_kind = "music_refresh"
        self.page._open_music_picker_after_load = True
        with patch("ui.douyin_commerce_page.QTimer.singleShot") as reopen:
            self.page._finish_immediate_write("music_refresh")

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

    def test_failed_immediate_music_write_discards_stale_dropdown_candidates(
        self,
    ) -> None:
        """平台回读失败后不能复用客户端暂存的失效候选。"""

        old_music = {"musicId": "music-old", "title": "旧音乐"}
        new_music = {"musicId": "music-new", "title": "新音乐"}
        self.page._selected_music = dict(old_music)
        self.page._pending_music = dict(new_music)
        self.page._show_music_candidates([old_music, new_music])

        self.page._immediate_music_failed("平台回读不一致")

        self.assertEqual(self.page._selected_music, old_music)
        self.assertEqual(self.page._music_candidates, [])
        self.assertIsNone(self.page.music_combo.currentData())
        self.assertIn("当前音乐：旧音乐", self.page.music_combo.currentText())

    def test_successful_music_write_keeps_public_candidates_for_next_change(
        self,
    ) -> None:
        """更换音乐无需手动刷新；下次写入会以当前平台抽屉重新核验。"""

        selected_music = {"musicId": "music-new", "title": "新音乐"}
        self.page._show_music_candidates(
            [{"musicId": "music-old", "title": "旧音乐"}, selected_music]
        )

        self.page._music_selected(selected_music)

        self.assertEqual(
            self.page._music_candidates,
            [
                {"musicId": "music-old", "title": "旧音乐", "creator": "", "duration": ""},
                {"musicId": "music-new", "title": "新音乐", "creator": "", "duration": ""},
            ],
        )
        self.assertEqual(self.page._music_candidate_source, "cache")
        self.assertEqual(self.page.music_combo.count(), 3)
        with patch.object(self.page, "_start_music_write") as write:
            self.page._music_combo_activated(1)
        write.assert_called_once_with(self.page._music_candidates[0])

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

    def test_platform_step_hint_explains_isolated_local_staging(self) -> None:
        """平台设置提示不得把三个采集器误说成同一编辑页。"""

        self.page.pages.setCurrentIndex(1)
        self.page._sync_view()

        hint = self.page.progress_context_label.text()
        self.assertIn("独立临时采集会话", hint)
        self.assertIn("本机暂存", hint)
        self.assertIn("正式发布时逐条重新核验", hint)
        self.assertNotIn("同一抖音编辑页", hint)

    def test_platform_workspace_orders_shared_settings_before_batch_locations(self) -> None:
        """音乐、声明、发布方式在左栏；右栏专用于逐条地点。"""

        left_layout = self.page.platform_left_column.layout()
        right_layout = self.page.platform_right_column.layout()

        self.assertIs(left_layout.itemAt(0).widget(), self.page.music_stage)
        self.assertIs(left_layout.itemAt(1).widget(), self.page.declaration_stage)
        self.assertIs(left_layout.itemAt(2).widget(), self.page.schedule_stage)
        self.assertIs(right_layout.itemAt(0).widget(), self.page.location_stage)
        self.assertIs(right_layout.itemAt(1).widget(), self.page.batch_item_settings_stage)

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
        """账号区含视频，内容区末尾保存本机内容，底栏只保留上传主操作。"""

        self.page.pages.setCurrentIndex(0)
        self.page._sync_view()

        account_column = self.page.findChild(QFrame, "douyinCommerceContentAccountColumn")
        content_column = self.page.findChild(QFrame, "douyinCommerceContentBodyColumn")
        log_column = self.page.findChild(QFrame, "douyinCommerceExecutionLog")

        self.assertIsNotNone(account_column)
        self.assertIsNotNone(content_column)
        self.assertIsNotNone(log_column)
        self.assertTrue(account_column.isAncestorOf(self.page.video_preview))
        self.assertTrue(content_column.isAncestorOf(self.page.restore_content_button))
        self.assertTrue(content_column.isAncestorOf(self.page.save_content_button))
        self.assertTrue(content_column.isAncestorOf(self.page.tag_history_host))
        self.assertEqual(self.page.video_replace_button.text(), "选择视频")
        self.assertIs(self.page.operation_dock_upload.parent(), self.page.operation_dock)
        self.assertFalse(self.page.operation_dock.isHidden())

    def test_content_cards_keep_uniform_primary_section_titles(self) -> None:
        """内容准备的账号、内容和视频栏必须使用同一套主标题样式。"""

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

    def test_submit_passes_created_task_id_to_the_same_editor_session(self) -> None:
        """验证挑战必须能以最终任务号回到原生客户端，不能丢失任务关联。"""

        payload = {"enableTimer": False, "runtimeMode": "publish", "debugDryRun": False}
        self.page._session_id = "commerce-session"
        self.page._preflight_fingerprint = ""
        task = {"id": 71, "taskNo": "T0806-0071"}
        with patch.object(self.page, "collect_payload", return_value=payload), patch.object(
            self.page, "_can_review", return_value=True
        ), patch(
            "ui.douyin_commerce_page.task_service.create_pending_task", return_value=task
        ), patch(
            "ui.douyin_commerce_page.task_service.mark_task_running"
        ), patch.object(self.page.runner, "run") as run, patch(
            "ui.douyin_commerce_page.douyin_commerce_session.commerce_session_manager.submit",
            return_value={"ok": True},
        ) as submit:
            self.page.open_submit_confirmation()
            run.call_args.args[1]()

        submit.assert_called_once_with("commerce-session", payload, 71)

    def test_batch_submit_starts_directly_without_a_second_confirmation_dialog(self) -> None:
        """点击批量确认提交后应直接创建任务，汇总信息已在检查页展示。"""

        payload = {"items": [{"mediaPath": "C:/demo.mp4"}]}
        task = {"id": 72, "taskNo": "T0806-0072"}
        with patch.object(self.page, "collect_batch_payload", return_value=payload), patch(
            "ui.douyin_commerce_page.task_service.create_douyin_batch_task",
            return_value=task,
        ), patch.object(self.page, "start_batch_publish") as start:
            self.page.open_batch_submit_confirmation()

        start.assert_called_once_with(payload, task)

    def test_verification_polling_deduplicates_a_task_dialog_and_clears_its_request(self) -> None:
        """重复轮询同一任务不得叠加对话框，任务结束必须清空内存请求。"""

        broker = DouyinVerificationBroker()
        request_id = broker.create_sms(task_id=72, message="需要短信验证")
        self.page._active_task_id = 72
        dialog = MagicMock()
        with patch("ui.douyin_commerce_page.verification_broker", broker), patch(
            "ui.douyin_commerce_page.DouyinVerificationDialog", return_value=dialog
        ) as dialog_type:
            self.page._start_douyin_verification_polling()
            self.page._poll_douyin_verification()
            self.page._poll_douyin_verification()

            dialog_type.assert_called_once_with(request_id, broker=broker, parent=self.page)
            self.page._submit_finished()

        self.assertIsNone(broker.request_for_task(72))
        self.assertFalse(self.page._douyin_verification_poll_timer.isActive())
        dialog.accept.assert_called_once()

    def test_content_preparation_keeps_aligned_three_column_geometry(self) -> None:
        """防止内容准备页退化为高度不齐、底部操作被挤走的通用表单。"""

        self.page.resize(1600, 900)
        self.page.show()
        self.app.processEvents()

        cards = [
            self.page.findChild(QFrame, "douyinCommerceContentAccountColumn"),
            self.page.findChild(QFrame, "douyinCommerceContentBodyColumn"),
            self.page.content_execution_log,
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

    def test_clear_current_content_keeps_saved_content_recoverable(self) -> None:
        """清空按钮只能清当前表单，不得删除本机已保存内容或启动平台操作。"""

        self.page.account_combo.addItem(
            "测试账号",
            {"id": 31, "type": 3, "status": 1, "filePath": "oneclick_3_demo.json"},
        )
        self.page.video_combo.addItem(
            "测试视频.mp4",
            {"id": 42, "storedPath": "/tmp/demo.mp4", "filename": "测试视频.mp4"},
        )
        self.page.account_combo.setCurrentIndex(self.page.account_combo.count() - 1)
        self.page.video_combo.setCurrentIndex(self.page.video_combo.count() - 1)
        self.page.title_input.setText("待清空标题")
        self.page.description_input.setPlainText("待清空文案")
        self.page._set_tags(["北海", "团购"])
        self.page._uploaded_editor_payload = {"title": "旧标题"}
        self.page._pending_upload_payload = {"title": "旧标题"}
        self.page._preflight_fingerprint = "旧预检"
        self.page._saved_content_available = True
        self.page._selected_music = {
            "musicId": "old-music",
            "title": "旧音乐",
            "creator": "旧作者",
            "duration": "00:30",
        }
        self.page._locations = [{"poiId": "old-poi"}]
        self.page._selected_location_data = {
            "poiId": "old-poi",
            "name": "旧地点",
            "address": "旧地址1号",
        }
        self.page._location_applied = True
        self.page._set_selected_declaration("内容由AI生成")
        self.page._confirmed_declaration = "内容由AI生成"
        self.page._batch_location_searches["__shared_location_search__"] = {
            "scope": "local",
            "keyword": "旧关键词",
            "candidates": [{"poiId": "old-poi"}],
        }
        self.page.batch_publish_mode.setCurrentIndex(
            self.page.batch_publish_mode.findData("interval-schedule")
        )

        with patch.object(self.page.runner, "run") as run:
            self.page.clear_current_content()

        self.assertEqual(self.page.account_combo.currentIndex(), 0)
        self.assertEqual(self.page.video_combo.currentIndex(), 0)
        self.assertEqual(self.page.title_input.text(), "")
        self.assertEqual(self.page.description_input.toPlainText(), "")
        self.assertEqual(self.page._tags(), [])
        self.assertIsNone(self.page._uploaded_editor_payload)
        self.assertIsNone(self.page._pending_upload_payload)
        self.assertEqual(self.page._preflight_fingerprint, "")
        self.assertIsNone(self.page._selected_music)
        self.assertEqual(self.page._locations, [])
        self.assertIsNone(self.page._selected_location_data)
        self.assertFalse(self.page._location_applied)
        self.assertEqual(
            self.page._selected_declaration(),
            self.page._DEFAULT_CONTENT_DECLARATION,
        )
        self.assertEqual(self.page._confirmed_declaration, "")
        self.assertEqual(
            self.page._batch_location_state(),
            {"scope": "domestic", "keyword": "", "candidates": []},
        )
        self.assertEqual(self.page.batch_publish_mode.currentData(), "immediate")
        self.assertTrue(self.page._saved_content_available)
        self.assertTrue(self.page.restore_content_button.isEnabled())
        run.assert_not_called()

    def test_clear_current_content_keeps_batch_saved_content_recoverable(self) -> None:
        """批量草稿独立保存；清空当前表单后仍必须能点击批量恢复。"""

        self.page._saved_content_available = False
        self.page._batch_saved_content_available = True
        self.page._selected_video_indexes = [0]

        self.page.clear_current_content()

        self.assertFalse(self.page._saved_content_available)
        self.assertTrue(self.page._batch_saved_content_available)
        self.assertTrue(self.page.batch_restore_content_button.isEnabled())

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

    def test_editing_content_after_review_clears_old_failure_and_offers_sync(self) -> None:
        """从检查页返回编辑标题、文案、标签时应同步，而不是保留失败或要求重传。"""

        payload = {
            "accountList": ["oneclick_3_demo.json"],
            "fileList": ["/tmp/demo.mp4"],
            "title": "原标题",
            "description": "原文案",
            "tags": ["北海"],
        }
        changed = dict(payload, description="修改后的文案")
        self.page._session_id = "session-demo"
        self.page._uploaded_editor_payload = dict(payload)
        self.page.pages.setCurrentIndex(0)
        self.page._set_stage_error("content", "上次同步失败")

        with patch.object(self.page, "collect_upload_payload", return_value=changed):
            self.page._content_changed()
            self.assertEqual(self.page._content_change_kind(), "sync")
            self.assertEqual(self.page.upload_button.text(), "同步内容并继续")

        self.assertTrue(self.page._stage_error_labels["content"].isHidden())

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
        self.assertEqual(self.page._locations, [])
        self.assertEqual(self.page.location_result_list.count(), 0)

    def test_location_write_failure_discards_stale_candidates_before_retry(self) -> None:
        """平台下拉已关闭时，旧候选不能继续被当作当前可点击项。"""

        self.page._show_locations([self.location_a])
        with patch.object(self.page, "_start_location_write"):
            self.page.location_result_list.setCurrentRow(0)
        with patch.object(self.page, "_platform_action_error"):
            self.page._immediate_location_failed("页面未找到唯一匹配的发布定位")

        self.assertEqual(self.page._locations, [])
        self.assertEqual(self.page.location_result_list.count(), 0)
        self.assertIs(
            self.page.location_view_stack.currentWidget(), self.page.location_search_view
        )

    def test_location_and_tag_placeholders_do_not_expose_specific_places(self) -> None:
        self.assertNotIn("北海", self.page.tags_input.placeholderText())
        self.assertNotIn("北海", self.page.location_keyword.placeholderText())
        self.assertNotIn("夜南香", self.page.location_keyword.placeholderText())

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

    def test_review_return_becomes_new_content_after_editor_session_ends(self) -> None:
        """提交完成关闭编辑会话后，检查页必须仍能回到内容准备。"""

        self.page.pages.setCurrentIndex(2)
        self.page._session_id = ""
        self.page.return_from_review()

        self.assertEqual(self.page.pages.currentIndex(), 0)
        self.assertEqual(self.page.review_back_button.text(), "开始新内容")

    def test_content_preparation_requires_account_video_and_description_only(self) -> None:
        self.page.title_input.clear()
        self.page._set_tags([])
        self.page.description_input.setPlainText("仅填写作品文案")
        with patch.object(self.page, "_selected_account", return_value={"id": 1}), patch.object(
            self.page, "_selected_video", return_value={"id": 2}
        ):
            self.assertTrue(self.page._content_is_valid())

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
        """平台进度位于状态与“返回内容/检查”按钮之间。"""

        layout = self.page.platform_review_dock.layout()

        self.assertIs(layout.itemAt(0).widget(), self.page.platform_review_status)
        self.assertIs(
            layout.itemAt(1).widget(),
            self.page.platform_collector_progress_frame,
        )
        self.assertIs(layout.itemAt(2).widget(), self.page.platform_back_button)
        self.assertIs(layout.itemAt(3).widget(), self.page.to_review_button)

    def test_platform_progress_is_rendered_in_bottom_action_dock(self) -> None:
        """平台处理状态不能额外占用工作区顶部，应显示在底部操作栏。"""

        self.page._immediate_write_kind = "music"
        self.page._sync_platform_workspace()

        self.assertIsNone(
            self.page.findChild(QFrame, "douyinCommercePlatformProgress")
        )
        self.assertTrue(self.page.platform_session_status.isHidden())
        self.assertEqual(self.page.platform_review_status.text(), "正在处理，请稍候")

    def test_review_footer_places_back_and_submit_on_the_right(self) -> None:
        """检查页的返回和确认提交必须归入统一底部操作栏右侧。"""

        layout = self.page.review_action_dock.layout()

        self.assertIs(layout.itemAt(0).widget(), self.page.validation_label)
        self.assertIs(layout.itemAt(1).widget(), self.page.review_back_button)
        self.assertIs(layout.itemAt(3).widget(), self.page.submit_button)
        self.assertIsNot(self.page.review_back_button.parentWidget(), self.page.review_submission_panel)
        self.assertIsNot(self.page.submit_button.parentWidget(), self.page.review_submission_panel)

    def test_review_footer_uses_concise_publish_hint(self) -> None:
        """检查页底部仅保留简洁的提交提示，避免冗长重复说明。"""

        self.page._selected_video_indexes = [1]
        with patch.object(
            self.page, "collect_batch_payload", return_value={"items": [{}]}
        ), patch.object(self.page, "_can_batch_review", return_value=True):
            self.page._sync_workbench()

        self.assertEqual(self.page.validation_label.text(), "确认信息后提交发布。")

    def test_all_stage_footers_share_height_layout_and_action_button_spec(self) -> None:
        """三个阶段的底部操作栏必须使用同一高度、内边距与按钮规格。"""

        footers = (
            self.page.operation_dock,
            self.page.platform_review_dock,
            self.page.review_action_dock,
        )
        for footer in footers:
            layout = footer.layout()
            self.assertEqual(footer.minimumHeight(), 82)
            self.assertEqual(footer.maximumHeight(), 82)
            self.assertEqual(layout.contentsMargins().left(), 22)
            self.assertEqual(layout.contentsMargins().top(), 15)
            self.assertEqual(layout.contentsMargins().right(), 20)
            self.assertEqual(layout.contentsMargins().bottom(), 15)
            self.assertEqual(layout.spacing(), 12)

        for action in (
            self.page.operation_dock_upload,
            self.page.platform_back_button,
            self.page.to_review_button,
            self.page.review_back_button,
            self.page.submit_button,
        ):
            self.assertTrue(action.property("footerAction"))

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

    def test_abandon_session_resets_all_platform_settings_but_keeps_content(self) -> None:
        """放弃上传后重新开始时，不得沿用上一批的音乐、地点、声明或定时。"""

        self.page._session_id = "session-demo"
        self.page.title_input.setText("保留的标题")
        self.page.description_input.setPlainText("保留的文案")
        self.page._selected_video_indexes = [1, 2]
        self.page._selected_music = {
            "musicId": "music-old",
            "title": "上一批音乐",
            "creator": "作者",
            "duration": "00:30",
        }
        self.page._batch_locations = {
            "/tmp/one.mp4": {
                "poiId": "poi-old",
                "name": "上一批地点",
                "address": "上一批完整地址 1 号",
                "scope": "local",
            }
        }
        self.page._batch_location_searches = {
            "__shared_location_search__": {
                "scope": "local",
                "keyword": "上一批关键词",
                "candidates": [{"poiId": "poi-old"}],
            }
        }
        self.page._batch_schedule_overrides = {
            "/tmp/one.mp4": "2026-08-12 18:00"
        }
        self.page.batch_location_scope_combo.setCurrentIndex(
            self.page.batch_location_scope_combo.findData("local")
        )
        self.page.batch_location_keyword.setText("上一批关键词")
        self.page._set_selected_declaration("内容由AI生成")
        self.page._confirmed_declaration = "内容由AI生成"
        self.page.timer_enabled.setChecked(True)
        self.page.batch_interval_minutes.setValue(45)

        with patch(
            "ui.douyin_commerce_page.douyin_commerce_session.commerce_session_manager.close"
        ):
            self.page._abandon_session(silent=True)

        self.assertEqual(self.page.title_input.text(), "保留的标题")
        self.assertEqual(self.page.description_input.toPlainText(), "保留的文案")
        self.assertEqual(self.page._selected_video_indexes, [1, 2])
        self.assertIsNone(self.page._selected_music)
        self.assertEqual(self.page._batch_locations, {})
        self.assertEqual(self.page._batch_schedule_overrides, {})
        self.assertEqual(
            self.page._batch_location_state(),
            {"scope": "domestic", "keyword": "", "candidates": []},
        )
        self.assertEqual(self.page.batch_location_scope_combo.currentData(), "domestic")
        self.assertEqual(self.page.batch_location_keyword.text(), "")
        self.assertEqual(
            self.page._selected_declaration(),
            self.page._DEFAULT_CONTENT_DECLARATION,
        )
        self.assertEqual(self.page._confirmed_declaration, "")
        self.assertFalse(self.page.timer_enabled.isChecked())
        self.assertEqual(self.page.batch_publish_mode.currentData(), "immediate")
        self.assertEqual(self.page.batch_interval_minutes.value(), 30)

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
    def test_strict_close_records_cancelled_step_and_continues_later_resources(self):
        context = MagicMock()
        context.close = AsyncMock(side_effect=asyncio.CancelledError())
        browser = MagicMock()
        browser.close = AsyncMock()
        playwright = MagicMock()
        playwright.stop = AsyncMock()
        manager = douyin_commerce_session.DouyinCommerceSessionManager()
        session = douyin_commerce_session._CommerceEditorSession(
            session_id="session-demo",
            upload_payload={},
            account_name="测试账号",
            browser=browser,
            context=context,
            page=object(),
            playwright=playwright,
            uploader=None,
        )
        manager._session = session

        with self.assertRaises(
            douyin_commerce_session.DouyinCommerceSessionError
        ) as raised:
            manager.close_strict("session-demo")

        self.assertEqual(str(raised.exception), "commerce_session_close_failed")
        self.assertIsNone(raised.exception.__cause__)
        self.assertIs(manager._session, session)
        context.close.assert_awaited_once_with()
        browser.close.assert_awaited_once_with()
        playwright.stop.assert_awaited_once_with()

        context.close.side_effect = None
        manager.close_strict("session-demo")

        self.assertIsNone(manager._session)
        self.assertEqual(context.close.await_count, 2)
        self.assertEqual(browser.close.await_count, 1)
        self.assertEqual(playwright.stop.await_count, 1)

    def test_strict_close_does_not_swallow_process_control_exceptions(self):
        for process_error in (KeyboardInterrupt(), SystemExit()):
            with self.subTest(error_type=type(process_error).__name__):
                context = MagicMock()
                context.close = AsyncMock(side_effect=process_error)
                browser = MagicMock()
                browser.close = AsyncMock()
                playwright = MagicMock()
                playwright.stop = AsyncMock()
                session = douyin_commerce_session._CommerceEditorSession(
                    session_id="session-demo",
                    upload_payload={},
                    account_name="测试账号",
                    browser=browser,
                    context=context,
                    page=object(),
                    playwright=playwright,
                    uploader=None,
                )

                with self.assertRaises(type(process_error)):
                    asyncio.run(
                        douyin_commerce_session.DouyinCommerceSessionManager._close_resources_strict(
                            session
                        )
                    )

                browser.close.assert_not_awaited()
                playwright.stop.assert_not_awaited()

    def test_strict_close_attempts_every_resource_and_keeps_failure_retryable(self):
        """严格关闭不得吞错，也不得因前项失败跳过后续资源。"""

        sensitive = "Cookie=secret 验证码123456 DOM=<html>private</html>"
        context = MagicMock()
        context.close = AsyncMock(side_effect=RuntimeError(sensitive))
        browser = MagicMock()
        browser.close = AsyncMock(
            side_effect=douyin_commerce_session.DouyinCommerceSessionError(
                sensitive
            )
        )
        playwright = MagicMock()
        playwright.stop = AsyncMock()
        manager = douyin_commerce_session.DouyinCommerceSessionManager()
        session = douyin_commerce_session._CommerceEditorSession(
            session_id="session-demo",
            upload_payload={},
            account_name="测试账号",
            browser=browser,
            context=context,
            page=object(),
            playwright=playwright,
            uploader=None,
        )
        manager._session = session
        strict_close = getattr(manager, "close_strict", None)
        self.assertTrue(callable(strict_close))

        with self.assertRaises(
            douyin_commerce_session.DouyinCommerceSessionError
        ) as raised:
            strict_close("session-demo")

        self.assertEqual(str(raised.exception), "commerce_session_close_failed")
        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn("secret", str(raised.exception))
        self.assertIs(manager._session, session)
        context.close.assert_awaited_once_with()
        browser.close.assert_awaited_once_with()
        playwright.stop.assert_awaited_once_with()

        context.close.side_effect = None
        browser.close.side_effect = None
        strict_close("session-demo")

        self.assertIsNone(manager._session)
        self.assertEqual(context.close.await_count, 2)
        self.assertEqual(browser.close.await_count, 2)
        self.assertEqual(playwright.stop.await_count, 1)

    def test_cached_music_is_read_only_and_scoped_to_current_account(self) -> None:
        """缓存读取不打开平台页面，只按当前已上传账号读取安全字段。"""

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
            account_id=31,
        )
        cached = [
            {
                "musicId": "music-001",
                "title": "收藏歌曲",
                "creator": "收藏作者",
                "duration": "03:21",
                "syncedAt": "2026-08-06T10:00:00+08:00",
            }
        ]

        with patch(
            "app_core.douyin_favorite_music_cache.list_cached_favorite_music",
            return_value=cached,
        ) as list_cached:
            result = asyncio.run(manager._cached_favorite_music("session-demo"))

        self.assertEqual(result, cached)
        list_cached.assert_called_once_with(31)

    def test_cached_music_selection_rejects_stale_current_list_item(self) -> None:
        """缓存命中后仍必须在当前抽屉精确匹配，过期项不可猜选。"""

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
            account_id=31,
        )
        current_candidate = {
            "musicId": "music-current",
            "title": "当前收藏歌曲",
            "creator": "当前作者",
            "duration": "03:21",
            "marker": "row-current",
        }
        with patch.object(
            douyin_commerce_session.douyin_music_service,
            "open_favorite_music_choices",
            new_callable=AsyncMock,
            return_value=(object(), object(), [current_candidate]),
        ):
            with self.assertRaisesRegex(
                douyin_commerce_session.DouyinCommerceSessionError,
                "缓存已过期",
            ):
                asyncio.run(
                    manager._select_cached_favorite_music("session-demo", "music-cached")
                )

        self.assertEqual(manager._session.music_candidates, [current_candidate])
        self.assertIsNotNone(manager._session.music_dialog)

    def test_manual_refresh_closes_picker_and_returns_current_account_cache(self) -> None:
        """刷新后必须关闭抽屉，地点等独立设置才可继续。"""

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
            account_id=31,
        )
        current = [
            {
                "musicId": "music-current",
                "title": "当前收藏歌曲",
                "creator": "当前作者",
                "duration": "03:21",
                "marker": "row-current",
            }
        ]
        cached = [
            {
                "musicId": "music-current",
                "title": "当前收藏歌曲",
                "creator": "当前作者",
                "duration": "03:21",
            }
        ]
        picker_page = object()
        dialog = object()
        with patch.object(
            douyin_commerce_session.douyin_music_service,
            "open_favorite_music_choices",
            new_callable=AsyncMock,
            return_value=(picker_page, dialog, current),
        ) as open_choices, patch.object(
            douyin_commerce_session.douyin_music_service,
            "close_favorite_music_choices",
            new_callable=AsyncMock,
        ) as close_picker, patch(
            "app_core.douyin_favorite_music_cache.replace_cached_favorite_music",
            return_value=cached,
        ) as replace:
            result = asyncio.run(manager._refresh_favorite_music("session-demo"))

        self.assertEqual(result[0]["musicId"], "music-current")
        open_choices.assert_awaited_once_with(manager._session.page)
        close_picker.assert_awaited_once_with(
            picker_page, dialog
        )
        replace.assert_called_once_with(
            31,
            [
                {
                    "musicId": "music-current",
                    "title": "当前收藏歌曲",
                    "creator": "当前作者",
                    "duration": "03:21",
                    "source": "douyin-favorite-visible",
                }
            ],
        )
        self.assertIsNone(manager._session.music_picker_page)
        self.assertIsNone(manager._session.music_dialog)

    def test_location_search_remains_available_after_music_refresh_closes_picker(self) -> None:
        """音乐刷新关闭抽屉后，同一编辑会话必须立即允许地点读取。"""

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
            account_id=31,
        )
        music = {
            "musicId": "music-001",
            "title": "收藏歌曲",
            "creator": "作者",
            "duration": "03:21",
        }
        locations = [{
            "name": "北海夜南香",
            "address": "广西壮族自治区北海市银海区银滩大道 1 号",
            "poiId": "visible-poi:test",
        }]
        with patch.object(
            douyin_commerce_session.douyin_music_service,
            "open_favorite_music_choices",
            new_callable=AsyncMock,
            return_value=(object(), object(), [music]),
        ), patch.object(
            douyin_commerce_session.douyin_music_service,
            "close_favorite_music_choices",
            new_callable=AsyncMock,
        ), patch(
            "app_core.douyin_favorite_music_cache.replace_cached_favorite_music",
            return_value=[music],
        ), patch.object(
            douyin_commerce_session.douyin_commerce_service,
            "search_commerce_location_store_candidates",
            new_callable=AsyncMock,
            return_value=locations,
        ) as search, patch.object(
            douyin_commerce_session.douyin_commerce_service,
            "close_commerce_store_selector",
            new_callable=AsyncMock,
        ):
            asyncio.run(manager._refresh_favorite_music("session-demo"))
            result = asyncio.run(
                manager._search_locations("session-demo", "北海夜南香", "domestic")
            )

        self.assertEqual(result, locations)
        self.assertIsNone(manager._session.music_dialog)
        self.assertIsNone(manager._session.music_picker_page)
        search.assert_awaited_once_with(
            manager._session.page, "北海夜南香", scope="domestic"
        )
        self.assertEqual(manager._session.music_candidates, [])

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

    def test_music_selection_failure_discards_transient_platform_candidates(self) -> None:
        """音乐弹窗回读失败后，不得将旧 marker 留给下一次更换操作。"""

        class OpenPage:
            def is_closed(self) -> bool:
                return False

        old_music = {"musicId": "music-old", "title": "旧音乐"}
        candidate = {"musicId": "music-new", "title": "新音乐", "marker": "row-1"}
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
            music_picker_page=object(),
            music_dialog=object(),
            music_candidates=[candidate],
            selected_music=old_music,
        )
        with patch.object(
            douyin_commerce_session.douyin_music_service,
            "select_favorite_music_choice",
            new_callable=AsyncMock,
            side_effect=douyin_music_service.DouyinMusicError("平台条目已刷新"),
        ):
            with self.assertRaisesRegex(
                douyin_commerce_session.DouyinCommerceSessionError,
                "未能选择并回读",
            ):
                asyncio.run(manager._select_favorite_music("session-demo", "music-new"))

        self.assertIsNone(manager._session.music_picker_page)
        self.assertIsNone(manager._session.music_dialog)
        self.assertEqual(manager._session.music_candidates, [])
        self.assertEqual(manager._session.selected_music, old_music)
        self.assertEqual(manager._session.stage, "music_selected")

    def test_music_close_failure_keeps_layer_for_publish_baseline_retry(self) -> None:
        """音乐已回读但抽屉未关闭时，基线不得跳过真实浮层返回 clean。"""

        class OpenPage:
            def is_closed(self) -> bool:
                return False

        class EmptyControls:
            async def count(self) -> int:
                return 0

        class VisibleDialog:
            def __init__(self) -> None:
                self.close_lookups = 0

            async def is_visible(self) -> bool:
                return True

            def locator(self, _selector: str) -> EmptyControls:
                self.close_lookups += 1
                return EmptyControls()

        manager = douyin_commerce_session.DouyinCommerceSessionManager()
        page = OpenPage()
        picker_page = object()
        dialog = VisibleDialog()
        old_music = {"musicId": "music-old", "title": "旧音乐"}
        candidate = {
            "musicId": "music-new",
            "title": "新音乐",
            "creator": "测试作者",
            "duration": "01:08",
            "marker": "row-1",
        }
        session = douyin_commerce_session._CommerceEditorSession(
            session_id="session-demo",
            upload_payload={},
            account_name="测试账号",
            browser=None,
            context=None,
            page=page,
            playwright=None,
            uploader=None,
            music_picker_page=picker_page,
            music_dialog=dialog,
            music_candidates=[candidate],
            selected_music=dict(old_music),
        )
        manager._session = session
        close_error = douyin_music_service.DouyinMusicError(
            "抖音收藏音乐已选中，但音乐抽屉无法安全关闭"
        )
        with patch.object(
            douyin_commerce_session.douyin_music_service,
            "select_favorite_music_choice",
            new_callable=AsyncMock,
            side_effect=close_error,
        ), patch.object(
            douyin_commerce_session.douyin_commerce_service,
            "close_commerce_store_selector",
            new_callable=AsyncMock,
        ) as close_location_selector:
            with self.assertRaisesRegex(
                douyin_commerce_session.DouyinCommerceSessionError,
                "未能选择并回读",
            ):
                asyncio.run(
                    manager._select_favorite_music("session-demo", "music-new")
                )
            with self.assertRaises(
                douyin_commerce_session.DouyinCommerceSessionError
            ) as raised:
                manager.prepare_publish_settings("session-demo")

        self.assertEqual(str(raised.exception), "正式发布页旧浮层未能清理")
        self.assertEqual(dialog.close_lookups, 2)
        close_location_selector.assert_not_awaited()
        self.assertIs(session.music_picker_page, picker_page)
        self.assertIs(session.music_dialog, dialog)
        self.assertEqual(session.music_candidates, [])
        self.assertEqual(session.selected_music, old_music)

    def test_favorite_music_selection_closes_picker_after_readback(self) -> None:
        """音乐仅在选中态回读后关闭抽屉，避免遮罩阻断其它编辑项。"""

        class ApplyButton:
            async def is_visible(self) -> bool:
                return True

            async def is_enabled(self) -> bool:
                return True

            async def inner_text(self) -> str:
                return "使用"

            async def scroll_into_view_if_needed(self, **_kwargs) -> None:
                return None

            async def click(self, **_kwargs) -> None:
                return None

        class ApplyButtons:
            async def count(self) -> int:
                return 1

            def nth(self, _index: int) -> ApplyButton:
                return ApplyButton()

        class Row:
            async def scroll_into_view_if_needed(self, **_kwargs) -> None:
                return None

            async def hover(self, **_kwargs) -> None:
                return None

            def locator(self, _selector: str) -> ApplyButtons:
                return ApplyButtons()

        class PickerPage:
            def locator(self, _selector: str) -> Row:
                return Row()

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        picker_page = PickerPage()
        dialog = object()
        candidate = {
            "musicId": "music-new",
            "title": "新音乐",
            "creator": "测试作者",
            "duration": "01:08",
            "marker": "row-1",
        }
        with patch.object(
            douyin_music_service,
            "_selection_is_readable",
            new_callable=AsyncMock,
            return_value=True,
        ), patch.object(
            douyin_music_service,
            "_close_selected_music_picker",
            new_callable=AsyncMock,
            return_value=None,
        ) as close_picker:
            selected = asyncio.run(
                douyin_music_service.select_favorite_music_choice(
                    object(), picker_page, dialog, candidate
                )
            )

        close_picker.assert_awaited_once_with(picker_page, dialog)
        self.assertNotIn("marker", selected)
        self.assertEqual(selected["musicId"], "music-new")

    def test_selected_music_picker_accepts_platform_auto_close(self) -> None:
        """平台点击“使用”后已自动关闭抽屉时，不应再误报关闭控件缺失。"""

        class AutoClosedDialog:
            async def is_visible(self) -> bool:
                return False

        class PickerPage:
            async def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        asyncio.run(
            douyin_music_service._close_selected_music_picker(
                PickerPage(), AutoClosedDialog()
            )
        )

    def test_selected_music_picker_uses_unique_accessible_modal_close_fallback(self) -> None:
        """新版弹层没有旧 sidesheet class 时，只接受弹层内唯一的明确关闭控件。"""

        class EmptyControls:
            async def count(self) -> int:
                return 0

        class Control:
            def __init__(self, dialog) -> None:
                self.dialog = dialog
                self.clicked = False

            async def is_visible(self) -> bool:
                return True

            async def is_enabled(self) -> bool:
                return True

            async def click(self, **_kwargs) -> None:
                self.clicked = True
                self.dialog.visible = False

        class Controls:
            def __init__(self, control) -> None:
                self.control = control

            async def count(self) -> int:
                return 1

            def nth(self, _index: int):
                return self.control

        class Dialog:
            def __init__(self) -> None:
                self.visible = True
                self.control = Control(self)

            async def is_visible(self) -> bool:
                return self.visible

            def locator(self, selector: str):
                if "semi-modal-close" in selector:
                    return Controls(self.control)
                return EmptyControls()

        class PickerPage:
            async def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        dialog = Dialog()
        asyncio.run(
            douyin_music_service._close_selected_music_picker(PickerPage(), dialog)
        )

        self.assertTrue(dialog.control.clicked)

    def test_selected_music_keeps_same_session_available_for_location_search(self) -> None:
        """首次刷新并选歌后，地点搜索继续复用同一编辑会话。"""

        class OpenPage:
            def is_closed(self) -> bool:
                return False

        editor_page = OpenPage()
        picker_page = object()
        dialog = object()
        candidate = {
            "musicId": "music-new",
            "title": "新音乐",
            "creator": "测试作者",
            "duration": "01:08",
            "marker": "row-1",
        }
        location = {
            "poiId": "poi-1",
            "name": "北海银滩景区",
            "address": "广西壮族自治区北海市银海区银滩大道中段",
            "distance": "6.0km",
        }
        manager = douyin_commerce_session.DouyinCommerceSessionManager()
        manager._session = douyin_commerce_session._CommerceEditorSession(
            session_id="session-demo",
            upload_payload={},
            account_name="测试账号",
            browser=None,
            context=None,
            page=editor_page,
            playwright=None,
            uploader=None,
            music_picker_page=picker_page,
            music_dialog=dialog,
            music_candidates=[candidate],
        )
        with patch.object(
            douyin_commerce_session.douyin_music_service,
            "select_favorite_music_choice",
            new_callable=AsyncMock,
            return_value={
                "musicId": "music-new",
                "title": "新音乐",
                "creator": "测试作者",
                "duration": "01:08",
            },
        ) as select, patch.object(
            douyin_commerce_session.douyin_commerce_service,
            "search_commerce_location_store_candidates",
            new_callable=AsyncMock,
            return_value=[location],
        ) as search, patch.object(
            douyin_commerce_session.douyin_commerce_service,
            "close_commerce_store_selector",
            new_callable=AsyncMock,
        ):
            # session 层在真实音乐服务关闭抽屉并回读后清理抽屉状态；此处验证
            # 该清理不会阻断同一 sessionId 的地点搜索。
            selected = asyncio.run(manager._select_favorite_music("session-demo", "music-new"))
            result = asyncio.run(manager._search_locations("session-demo", "北海", "domestic"))

        select.assert_awaited_once_with(editor_page, picker_page, dialog, candidate)
        # 音乐服务关闭抽屉后，session 不得遗留抽屉状态。
        self.assertIsNone(manager._session.music_picker_page)
        self.assertIsNone(manager._session.music_dialog)
        search.assert_awaited_once_with(editor_page, "北海", scope="domestic")
        self.assertEqual(selected["musicId"], "music-new")
        self.assertEqual(result, [location])

    def test_music_replace_after_refresh_leaves_no_picker_before_location_search(self) -> None:
        """覆盖真实顺序：选音乐 A → 刷新 → 选音乐 B → 搜索地点。"""

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
            account_id=31,
        )
        music_a = {"musicId": "music-a", "title": "音乐 A", "creator": "作者", "duration": "03:21", "marker": "a"}
        music_b = {"musicId": "music-b", "title": "音乐 B", "creator": "作者", "duration": "03:22", "marker": "b"}
        location = {"poiId": "poi-1", "name": "北海夜南香", "address": "广西壮族自治区北海市银海区银滩大道 1 号"}
        pickers = [(object(), object(), [music_a, music_b]) for _ in range(3)]

        async def select(_page, _picker, _dialog, candidate):
            return {key: value for key, value in candidate.items() if key != "marker"}

        with patch.object(
            douyin_commerce_session.douyin_music_service,
            "open_favorite_music_choices",
            new_callable=AsyncMock,
            side_effect=pickers,
        ) as open_picker, patch.object(
            douyin_commerce_session.douyin_music_service,
            "select_favorite_music_choice",
            new_callable=AsyncMock,
            side_effect=select,
        ) as select_picker, patch.object(
            douyin_commerce_session.douyin_music_service,
            "close_favorite_music_choices",
            new_callable=AsyncMock,
        ) as close_picker, patch(
            "app_core.douyin_favorite_music_cache.replace_cached_favorite_music",
            return_value=[music_a, music_b],
        ), patch.object(
            douyin_commerce_session.douyin_commerce_service,
            "search_commerce_location_store_candidates",
            new_callable=AsyncMock,
            return_value=[location],
        ) as search, patch.object(
            douyin_commerce_session.douyin_commerce_service,
            "close_commerce_store_selector",
            new_callable=AsyncMock,
        ):
            asyncio.run(manager._select_cached_favorite_music("session-demo", "music-a"))
            asyncio.run(manager._refresh_favorite_music("session-demo"))
            asyncio.run(manager._select_cached_favorite_music("session-demo", "music-b"))
            result = asyncio.run(manager._search_locations("session-demo", "北海夜南香", "domestic"))

        self.assertEqual(result, [location])
        self.assertEqual(open_picker.await_count, 3)
        self.assertEqual(select_picker.await_count, 2)
        self.assertEqual(close_picker.await_count, 1)
        self.assertIsNone(manager._session.music_picker_page)
        self.assertIsNone(manager._session.music_dialog)
        self.assertEqual(manager._session.selected_music["musicId"], "music-b")
        search.assert_awaited_once_with(manager._session.page, "北海夜南香", scope="domestic")

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
            {"headless": True, "hide_until_ready": False},
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
        ) as search, patch.object(
            douyin_commerce_session.douyin_commerce_service,
            "close_commerce_store_selector",
            new_callable=AsyncMock,
        ):
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

                self.apply_sms_verification_code = AsyncMock()

                async def wait_for_receipt(_page, on_verification):
                    self.background_mode_at_receipt = base_social_media.is_publish_background_mode()
                    await on_verification(
                        VerificationChallenge(
                            kind="sms",
                            message="请在一键发客户端输入短信验证码",
                        )
                    )
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

        class VerificationBroker:
            def __init__(self) -> None:
                self.sms_task_id = None
                self.succeeded = []
                self.cleared = []

            def create_sms(self, *, task_id: int, message: str, **_kwargs) -> str:
                self.sms_task_id = task_id
                return "request-demo"

            def snapshot(self, request_id: str) -> dict:
                if request_id != "request-demo":
                    raise AssertionError("验证请求标识不匹配")
                return {"state": "waiting"}

            def consume_code(self, request_id: str):
                if request_id != "request-demo":
                    raise AssertionError("验证请求标识不匹配")
                return "123456"

            def claim_code(self, request_id: str):
                return self.consume_code(request_id)

            def ensure_processing(self, request_id: str) -> None:
                if request_id != "request-demo":
                    raise AssertionError("验证请求标识不匹配")

            def succeed(self, request_id: str) -> None:
                self.succeeded.append(request_id)

            def clear(self, request_id: str) -> None:
                self.cleared.append(request_id)

        broker = VerificationBroker()
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
        ) as scheduled_readback, patch.object(
            douyin_commerce_session,
            "verification_broker",
            broker,
            create=True,
        ):
            result = asyncio.run(manager._submit("session-demo", payload, task_id=77))

        reveal.assert_not_awaited()
        self.assertTrue(uploader.background_mode_at_receipt)
        uploader.set_schedule_time_douyin.assert_not_awaited()
        publish_button.click.assert_awaited_once_with(timeout=10_000)
        scheduled_readback.assert_not_awaited()
        self.assertFalse(result["scheduled"])
        self.assertIsNone(result["scheduledAt"])
        self.assertEqual(result["platformReceipt"], {"status": "published"})
        self.assertEqual(broker.sms_task_id, 77)
        uploader.apply_sms_verification_code.assert_awaited_once()
        self.assertEqual(broker.succeeded, ["request-demo"])
        self.assertEqual(broker.cleared, ["request-demo"])
        self.assertIsNone(manager._session)

    def test_publish_verification_writes_explicit_runtime_log(self) -> None:
        """最终提交若出现短信验证，运行日志必须明确记录触发与通过。"""

        class Page:
            async def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        class Uploader:
            def __init__(self) -> None:
                self.apply_sms_verification_code = AsyncMock()

        class Broker:
            def create_sms(self, **_kwargs) -> str:
                return "request-log"

            def snapshot(self, _request_id: str) -> dict:
                return {"state": "waiting"}

            def claim_code(self, _request_id: str) -> str:
                return "123456"

            def ensure_processing(self, _request_id: str) -> None:
                return None

            def succeed(self, _request_id: str) -> None:
                return None

            def clear(self, _request_id: str) -> None:
                return None

        session = douyin_commerce_session._CommerceEditorSession(
            session_id="session-log",
            upload_payload={},
            account_name="测试账号",
            browser=None,
            context=None,
            page=Page(),
            playwright=None,
            uploader=Uploader(),
        )
        messages: list[str] = []
        sink_id = logger.add(lambda message: messages.append(message.record["message"]))
        try:
            with patch.object(douyin_commerce_session, "verification_broker", Broker()):
                asyncio.run(
                    douyin_commerce_session.DouyinCommerceSessionManager()._handle_publish_verification(
                        session,
                        VerificationChallenge(
                            kind="sms",
                            message="请在一键发客户端输入短信验证码",
                        ),
                        task_id=81,
                    )
                )
        finally:
            logger.remove(sink_id)

        self.assertTrue(any("触发短信验证码" in message for message in messages))
        self.assertTrue(any("短信验证码已通过" in message for message in messages))

    def test_cancelled_or_expired_sms_claim_stops_before_page_write_or_receipt(self) -> None:
        """验证码被领取后若已取消或超时，不能再填写、点击或返回成功回执。"""

        class Page:
            async def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        class Uploader:
            def __init__(self) -> None:
                self.apply_sms_verification_code = AsyncMock()

        class Broker:
            def __init__(self, state: str) -> None:
                self.state = state
                self.cleared = []

            def create_sms(self, **_kwargs) -> str:
                return "request-race"

            def snapshot(self, _request_id: str) -> dict:
                return {"state": "waiting"}

            def consume_code(self, _request_id: str) -> str:
                return "123456"

            def claim_code(self, _request_id: str) -> str:
                return "123456"

            def ensure_processing(self, _request_id: str) -> None:
                raise RuntimeError(self.state)

            def succeed(self, _request_id: str) -> None:
                return None

            def fail(self, _request_id: str) -> None:
                return None

            def clear(self, request_id: str) -> None:
                self.cleared.append(request_id)

        for state in ("cancelled", "expired"):
            with self.subTest(state=state):
                uploader = Uploader()
                manager = douyin_commerce_session.DouyinCommerceSessionManager()
                session = douyin_commerce_session._CommerceEditorSession(
                    session_id="session-race",
                    upload_payload={},
                    account_name="测试账号",
                    browser=None,
                    context=None,
                    page=Page(),
                    playwright=None,
                    uploader=uploader,
                )
                broker = Broker(state)
                with patch.object(
                    douyin_commerce_session,
                    "verification_broker",
                    broker,
                ):
                    with self.assertRaisesRegex(
                        douyin_commerce_session.DouyinCommerceSessionError,
                        "验证",
                    ):
                        asyncio.run(
                            manager._handle_publish_verification(
                                session,
                                VerificationChallenge(
                                    kind="sms",
                                    message="请在一键发客户端输入短信验证码",
                                ),
                                task_id=78,
                            )
                        )

                uploader.apply_sms_verification_code.assert_not_awaited()
                self.assertEqual(broker.cleared, ["request-race"])

    def test_verification_without_task_id_stops_before_broker_or_page_write(self) -> None:
        class Page:
            async def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        class Uploader:
            apply_sms_verification_code = AsyncMock()

        manager = douyin_commerce_session.DouyinCommerceSessionManager()
        session = douyin_commerce_session._CommerceEditorSession(
            session_id="session-no-task",
            upload_payload={},
            account_name="测试账号",
            browser=None,
            context=None,
            page=Page(),
            playwright=None,
            uploader=Uploader(),
        )
        with self.assertRaisesRegex(
            douyin_commerce_session.DouyinCommerceSessionError,
            "缺少任务号",
        ):
            asyncio.run(
                manager._handle_publish_verification(
                    session,
                    VerificationChallenge(
                        kind="sms",
                        message="请在一键发客户端输入短信验证码",
                    ),
                    task_id=None,
                )
            )
        session.uploader.apply_sms_verification_code.assert_not_awaited()

    def test_rejected_sms_code_returns_to_same_request_for_reentry(self) -> None:
        """平台明确拒绝验证码时，应保留同一会话并允许重新输入一次。"""

        class Page:
            async def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        class Uploader:
            apply_sms_verification_code = AsyncMock(
                side_effect=[RuntimeError("验证码被平台拒绝"), None]
            )

        class Broker:
            def __init__(self) -> None:
                self.state = "waiting"
                self.codes = ["123456", "654321"]
                self.retried = []
                self.succeeded = []
                self.failed = []

            def create_sms(self, **_kwargs) -> str:
                return "request-rejected"

            def snapshot(self, _request_id: str) -> dict:
                return {"state": self.state}

            def claim_code(self, _request_id: str) -> str:
                if self.state != "waiting" or not self.codes:
                    return ""
                self.state = "processing"
                return self.codes.pop(0)

            def ensure_processing(self, _request_id: str) -> None:
                return None

            def fail(self, request_id: str) -> None:
                self.failed.append(request_id)

            def retry_sms_input(self, request_id: str) -> None:
                self.retried.append(request_id)
                self.state = "waiting"

            def succeed(self, request_id: str) -> None:
                self.succeeded.append(request_id)
                self.state = "success"

            def clear(self, _request_id: str) -> None:
                return None

        uploader = Uploader()
        session = douyin_commerce_session._CommerceEditorSession(
            session_id="session-rejected",
            upload_payload={},
            account_name="测试账号",
            browser=None,
            context=None,
            page=Page(),
            playwright=None,
            uploader=uploader,
        )
        broker = Broker()
        with patch.object(douyin_commerce_session, "verification_broker", broker):
            asyncio.run(
                douyin_commerce_session.DouyinCommerceSessionManager()._handle_publish_verification(
                    session,
                    VerificationChallenge(
                        kind="sms",
                        message="请在一键发客户端输入短信验证码",
                    ),
                    task_id=79,
                )
            )

        self.assertEqual(uploader.apply_sms_verification_code.await_count, 2)
        self.assertEqual(broker.retried, ["request-rejected"])
        self.assertEqual(broker.succeeded, ["request-rejected"])
        self.assertEqual(broker.failed, [])

    def test_sms_failure_message_distinguishes_write_control_and_platform_results(self) -> None:
        """客户端提示必须回答“平台明确拒绝”还是“尚不能确认”。"""

        message_for = douyin_commerce_session._sms_verification_failure_message
        self.assertEqual(
            message_for(RuntimeError("抖音页面明确提示验证码错误或已过期，发布未继续")),
            "抖音页面明确提示验证码错误或已过期，发布未继续。",
        )
        self.assertEqual(
            message_for(RuntimeError("抖音验证码填写后未能回读，发布已安全停止")),
            "验证码未能写入抖音验证输入框，发布未继续。",
        )
        self.assertEqual(
            message_for(RuntimeError("抖音验证码已写入，但验证按钮未启用；无法判断验证码是否正确，发布已安全停止")),
            "验证码已写入，但验证按钮未启用；无法判断验证码是否正确，发布未继续。",
        )
        self.assertEqual(
            message_for(RuntimeError("请求超时")),
            "验证码已提交，但抖音未返回可确认结果；无法判断验证码是否正确，发布未继续。",
        )

    def test_sms_resend_returns_to_the_same_running_editor_session(self) -> None:
        """60 秒后重发必须回到同一 Playwright 会话，不能新建验证码请求。"""

        now = [100.0]
        request_ready = threading.Event()
        worker_errors: list[Exception] = []

        class Page:
            async def wait_for_timeout(self, _milliseconds: int) -> None:
                await asyncio.sleep(0.005)

        class Uploader:
            def __init__(self) -> None:
                self.resend_calls: list[tuple[object, object]] = []

            async def resend_sms_verification_code(self, page, challenge) -> None:
                self.resend_calls.append((page, challenge))

        class Broker(douyin_verification.DouyinVerificationBroker):
            def __init__(self) -> None:
                super().__init__(clock=lambda: now[0])
                self.request_id = ""

            def create_sms(self, **kwargs) -> str:
                self.request_id = super().create_sms(**kwargs)
                request_ready.set()
                return self.request_id

        broker = Broker()
        page = Page()
        uploader = Uploader()
        session = douyin_commerce_session._CommerceEditorSession(
            session_id="session-resend",
            upload_payload={},
            account_name="测试账号",
            browser=None,
            context=None,
            page=page,
            playwright=None,
            uploader=uploader,
        )

        def request_resend_then_cancel() -> None:
            try:
                if not request_ready.wait(timeout=1):
                    raise AssertionError("短信验证请求未创建")
                now[0] = 160.0
                broker.request_sms_resend(broker.request_id)
                broker.cancel(broker.request_id)
            except Exception as exc:
                worker_errors.append(exc)

        worker = threading.Thread(target=request_resend_then_cancel)
        worker.start()
        try:
            with patch.object(douyin_commerce_session, "verification_broker", broker):
                with self.assertRaisesRegex(
                    douyin_commerce_session.DouyinCommerceSessionError,
                    "验证已取消",
                ):
                    asyncio.run(
                        douyin_commerce_session.DouyinCommerceSessionManager()._handle_publish_verification(
                            session,
                            VerificationChallenge(kind="sms", message="需要短信验证码"),
                            task_id=81,
                        )
                    )
        finally:
            worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(worker_errors, [])
        self.assertEqual(len(uploader.resend_calls), 1)
        self.assertIs(uploader.resend_calls[0][0], page)
        self.assertEqual(broker.request_for_task(81), None)

    def test_sms_processing_is_atomic_across_fill_click_cancel_and_expiry(self) -> None:
        """真实 await 交错时，fill/click 期间的取消与 wait 超时不能撕裂事务。"""

        class Controls:
            def __init__(self, items) -> None:
                self.items = list(items)

            async def count(self) -> int:
                return len(self.items)

            def nth(self, index: int):
                return self.items[index]

        class Textbox:
            def __init__(self, page) -> None:
                self.page = page
                self.value = ""

            async def is_visible(self) -> bool:
                return True

            async def is_enabled(self) -> bool:
                return True

            async def fill(self, value: str) -> None:
                await self.page.pause_for_external_race("fill")
                self.value = value

            async def input_value(self) -> str:
                return self.value

        class ConfirmButton:
            def __init__(self, page) -> None:
                self.page = page

            async def is_visible(self) -> bool:
                return True

            async def is_enabled(self) -> bool:
                return True

            async def click(self, *, timeout: int) -> None:
                del timeout
                await self.page.pause_for_external_race("click")
                self.page.url = "https://creator.douyin.com/creator-micro/content/manage"

        class VerificationContainer:
            def __init__(self, page) -> None:
                self.page = page

            async def is_visible(self) -> bool:
                return True

            async def evaluate(self, _script: str) -> str:
                return "atomic-sms-verification-container"

            def get_by_role(self, role: str, **_kwargs):
                if role == "textbox":
                    return Controls([self.page.textbox])
                if role == "button":
                    return Controls([self.page.confirm])
                return Controls([])

        class Marker:
            def __init__(self, container) -> None:
                self.container = container

            async def is_visible(self) -> bool:
                return True

            def locator(self, _selector: str):
                return Controls([self.container])

        class Page:
            def __init__(self, broker) -> None:
                self.broker = broker
                self.cancel_results = []
                self.wait_states = []
                self.states_during_pause = []
                self.race_errors = []
                self.race_threads = []
                self.url = "https://creator.douyin.com/verification"
                self.textbox = Textbox(self)
                self.confirm = ConfirmButton(self)
                self.container = VerificationContainer(self)
                self.marker = Marker(self.container)

            async def pause_for_external_race(self, phase: str) -> None:
                paused = threading.Event()
                release = threading.Event()

                def race_worker() -> None:
                    try:
                        if not paused.wait(timeout=2):
                            raise AssertionError(f"{phase} 未进入 await 暂停")
                        self.cancel_results.append(
                            self.broker.cancel(self.broker.request_id)
                        )
                        self.wait_states.append(
                            self.broker.wait(
                                self.broker.request_id,
                                timeout_seconds=0.01,
                            )["state"]
                        )
                        self.states_during_pause.append(
                            self.broker.snapshot(self.broker.request_id)["state"]
                        )
                    except Exception as exc:  # 测试线程中的错误必须回传主线程。
                        self.race_errors.append(exc)
                    finally:
                        release.set()

                worker = threading.Thread(target=race_worker)
                self.race_threads.append(worker)
                worker.start()
                paused.set()
                released = await asyncio.to_thread(release.wait, 2)
                if not released:
                    raise AssertionError(f"{phase} 的并发控制线程未释放 await")

            def join_races(self) -> None:
                for worker in self.race_threads:
                    worker.join(timeout=1)
                    if worker.is_alive():
                        self.race_errors.append(AssertionError("并发控制线程未结束"))

            def get_by_text(self, text: str, *, exact: bool):
                if self.url.endswith("/verification") and text == "接收短信验证码" and exact:
                    return Controls([self.marker])
                return Controls([])

            def get_by_role(self, role: str, **_kwargs):
                if role == "textbox":
                    return Controls([self.textbox])
                if role == "button":
                    return Controls([self.confirm])
                return Controls([])

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        class Broker(douyin_verification.DouyinVerificationBroker):
            def __init__(self) -> None:
                super().__init__()
                self.request_id = ""
                self.success_states = []

            def create_sms(self, **kwargs) -> str:
                request_id = super().create_sms(**kwargs, expires_in_seconds=600)
                self.request_id = request_id
                self.submit_code(request_id, "123456")
                return request_id

            def succeed(self, request_id: str) -> None:
                super().succeed(request_id)
                self.success_states.append(self.snapshot(request_id)["state"])

        broker = Broker()
        page = Page(broker)
        uploader = DouYinVideo(
            title="测试标题",
            file_path="/tmp/demo.mp4",
            tags=[],
            publish_date=datetime.now(),
            account_file="/tmp/account.json",
            description="测试文案",
        )
        session = douyin_commerce_session._CommerceEditorSession(
            session_id="session-atomic",
            upload_payload={},
            account_name="测试账号",
            browser=None,
            context=None,
            page=page,
            playwright=None,
            uploader=uploader,
        )
        try:
            with patch.object(douyin_commerce_session, "verification_broker", broker):
                asyncio.run(
                    douyin_commerce_session.DouyinCommerceSessionManager()._handle_publish_verification(
                        session,
                        VerificationChallenge(
                            kind="sms",
                            message="请在一键发客户端输入短信验证码",
                        ),
                        task_id=80,
                    )
                )
        finally:
            page.join_races()

        self.assertEqual(page.cancel_results, [False, False])
        self.assertEqual(page.wait_states, ["processing", "processing"])
        self.assertEqual(page.states_during_pause, ["processing", "processing"])
        self.assertEqual(page.race_errors, [])
        self.assertEqual(broker.success_states, ["success"])

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
        ) as search, patch.object(
            douyin_commerce_session.douyin_commerce_service,
            "close_commerce_store_selector",
            new_callable=AsyncMock,
        ) as close_selector:
            result = asyncio.run(
                manager._search_locations("session-demo", "北海", "domestic")
            )

        search.assert_awaited_once_with(manager._session.page, "北海", scope="domestic")
        self.assertEqual(close_selector.await_count, 2)
        self.assertEqual(result, expected)
        self.assertEqual(manager._session.commerce_location_candidates, expected)
        self.assertIsNone(manager._session.location)
        self.assertEqual(manager._session.location_scope, "domestic")

    def test_apply_saved_location_updates_session_only_after_atomic_readback(self) -> None:
        """会话层必须把预设和有界关键词一次交给 DOM 原子动作。"""

        class OpenPage:
            def is_closed(self) -> bool:
                return False

        class Uploader:
            location_verification = ""

        location = {
            "poiId": "poi-001",
            "name": "北海银滩景区",
            "address": "广西壮族自治区北海市银海区银滩大道中段",
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
            uploader=Uploader(),
            stage="declaration_selected",
            selected_declaration="无需添加自主声明",
            stores=[{"storeId": "stale"}],
            selected_store={"storeId": "stale"},
            preflight_fingerprint="stale",
            schedule_time="2026-08-11 09:00",
        )
        keywords = [location["address"], "北海 北海银滩景区"]
        with patch.object(
            douyin_commerce_session.douyin_commerce_service,
            "apply_saved_commerce_location_to_page",
            new_callable=AsyncMock,
            return_value={
                "location": {**location, "distance": ""},
                "matchedKeyword": keywords[1],
            },
        ) as apply_atomic:
            result = manager.apply_saved_location(
                "session-demo",
                location,
                "domestic",
                keywords,
            )

        self.assertEqual(apply_atomic.await_count, 1)
        actual_page, actual_preset, actual_scope, actual_keywords = (
            apply_atomic.await_args.args
        )
        self.assertIs(actual_page, manager._session.page)
        self.assertEqual(actual_preset["name"], location["name"])
        self.assertEqual(actual_preset["address"], location["address"])
        self.assertTrue(actual_preset["poiId"].startswith("visible-poi:"))
        self.assertEqual(actual_scope, "domestic")
        self.assertEqual(actual_keywords, keywords)
        self.assertTrue(result["location"]["poiId"].startswith("visible-poi:"))
        self.assertEqual(result["matchedKeyword"], keywords[1])
        self.assertEqual(manager._session.location_scope, "domestic")
        self.assertEqual(
            manager._session.commerce_location_candidates,
            [result["location"]],
        )
        self.assertEqual(manager._session.stores, [])
        self.assertIsNone(manager._session.selected_store)
        self.assertEqual(manager._session.preflight_fingerprint, "")
        self.assertEqual(manager._session.schedule_time, "")
        self.assertEqual(manager._session.uploader.location_verification, location["name"])

    def test_apply_saved_location_rejects_missing_keywords_before_dom_action(self) -> None:
        """空关键词不得打开正式发布页的地点面板。"""

        manager = douyin_commerce_session.DouyinCommerceSessionManager()
        location = {
            "poiId": "poi-001",
            "name": "北海银滩景区",
            "address": "广西壮族自治区北海市银海区银滩大道中段",
        }
        with patch.object(
            douyin_commerce_session.douyin_commerce_service,
            "apply_saved_commerce_location_to_page",
            new_callable=AsyncMock,
        ) as apply_atomic:
            with self.assertRaisesRegex(
                douyin_commerce_session.DouyinCommerceSessionError,
                "publish_location_candidate_missing",
            ):
                manager.apply_saved_location(
                    "session-demo",
                    location,
                    "domestic",
                    ["", "  "],
                )

        apply_atomic.assert_not_awaited()

    def test_location_search_failure_still_closes_candidate_selector(self) -> None:
        """读取失败也必须收口地点浮层，避免下一次搜索继承残留菜单。"""

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
        )
        with patch.object(
            douyin_commerce_session.douyin_commerce_service,
            "search_commerce_location_store_candidates",
            new_callable=AsyncMock,
            side_effect=douyin_commerce_service.DouyinCommerceError("候选读取失败"),
        ), patch.object(
            douyin_commerce_session.douyin_commerce_service,
            "close_commerce_store_selector",
            new_callable=AsyncMock,
        ) as close_selector:
            with self.assertRaisesRegex(
                douyin_commerce_session.DouyinCommerceSessionError,
                "候选读取失败",
            ):
                asyncio.run(
                    manager._search_locations("session-demo", "北海", "domestic")
                )

        self.assertEqual(close_selector.await_count, 2)

    def test_location_search_never_refreshes_music(self) -> None:
        """地点采集不得因任何历史标记触发收藏音乐刷新。"""

        class OpenPage:
            wait_for_timeout = AsyncMock()

            def is_closed(self) -> bool:
                return False

        manager = douyin_commerce_session.DouyinCommerceSessionManager()
        page = OpenPage()
        session = douyin_commerce_session._CommerceEditorSession(
            session_id="session-demo",
            upload_payload={},
            account_name="测试账号",
            browser=None,
            context=None,
            page=page,
            playwright=None,
            uploader=None,
        )
        # 覆盖旧进程可能残留的动态属性；新实现不得读取它。
        session.platform_dom_needs_music_warmup = True
        manager._session = session
        expected = [
            {
                "poiId": "poi-night-south",
                "name": "夜南香北京烤鸭",
                "address": "广西壮族自治区北海市银海区银滩大道 1 号",
                "distance": "1.2km",
            }
        ]
        with patch.object(
            manager,
            "_refresh_favorite_music",
            new_callable=AsyncMock,
            return_value=[],
        ) as refresh_music, patch.object(
            douyin_commerce_session.douyin_commerce_service,
            "close_commerce_store_selector",
            new_callable=AsyncMock,
        ), patch.object(
            douyin_commerce_session.douyin_commerce_service,
            "search_commerce_location_store_candidates",
            new_callable=AsyncMock,
            return_value=expected,
        ):
            result = asyncio.run(
                manager._search_locations("session-demo", "北海夜南香", "domestic")
            )

        self.assertEqual(result[0]["name"], "夜南香北京烤鸭")
        refresh_music.assert_not_awaited()
        page.wait_for_timeout.assert_not_awaited()

    def test_transient_missing_scope_panel_waits_then_retries_without_music(self) -> None:
        """范围面板短暂未挂载时只允许有界等待并重试地点。"""

        class OpenPage:
            wait_for_timeout = AsyncMock()

            def is_closed(self) -> bool:
                return False

        manager = douyin_commerce_session.DouyinCommerceSessionManager()
        page = OpenPage()
        manager._session = douyin_commerce_session._CommerceEditorSession(
            session_id="session-demo",
            upload_payload={},
            account_name="测试账号",
            browser=None,
            context=None,
            page=page,
            playwright=None,
            uploader=None,
        )
        expected = [{"name": "北海夜南香", "address": "广西北海市银海区示例路1号"}]
        transient_error = douyin_commerce_service.DouyinCommerceError(
            "抖音位置搜索范围‘本地/国内’控件未能唯一显示"
            "（当前地点面板内本地 0 个、国内 0 个；pair-not-in-search-panel），已安全停止"
        )
        with patch.object(
            manager,
            "_refresh_favorite_music",
            new_callable=AsyncMock,
            return_value=[],
        ) as refresh_music, patch.object(
            douyin_commerce_session.douyin_commerce_service,
            "close_commerce_store_selector",
            new_callable=AsyncMock,
        ) as close_selector, patch.object(
            douyin_commerce_session.douyin_commerce_service,
            "search_commerce_location_store_candidates",
            new_callable=AsyncMock,
            side_effect=[transient_error, expected],
        ) as search:
            result = asyncio.run(
                manager._search_locations("session-demo", "北海夜南香", "domestic")
            )

        self.assertEqual(search.await_count, 2)
        refresh_music.assert_not_awaited()
        page.wait_for_timeout.assert_awaited_once_with(1_500)
        self.assertEqual(close_selector.await_count, 4)
        self.assertEqual(result, expected)

    def test_prepare_publish_settings_closes_layers_before_clearing_state(self) -> None:
        """正式页基线必须先关闭真实浮层，再清理内存旧值。"""

        class OpenPage:
            def is_closed(self) -> bool:
                return False

        manager = douyin_commerce_session.DouyinCommerceSessionManager()
        page = OpenPage()
        picker_page = object()
        dialog = object()
        session = douyin_commerce_session._CommerceEditorSession(
            session_id="session-demo",
            upload_payload={},
            account_name="测试账号",
            browser=None,
            context=None,
            page=page,
            playwright=None,
            uploader=None,
            stage="preflighted",
            music_picker_page=picker_page,
            music_dialog=dialog,
            music_candidates=[{"musicId": "music-old", "title": "旧音乐"}],
            selected_music={"musicId": "music-old", "title": "旧音乐"},
            commerce_location_candidates=[
                {
                    "poiId": "poi-old",
                    "name": "旧地点",
                    "address": "广西壮族自治区北海市银海区旧路 1 号",
                }
            ],
            location={
                "poiId": "poi-old",
                "name": "旧地点",
                "address": "广西壮族自治区北海市银海区旧路 1 号",
            },
            location_scope="local",
            selected_declaration="内容由AI生成",
            stores=[{"storeId": "store-old", "name": "旧门店"}],
            selected_store={"storeId": "store-old", "name": "旧门店"},
            preflight_fingerprint="old-fingerprint",
            schedule_time="2026-08-10 16:00",
        )
        manager._session = session
        close_order: list[str] = []

        async def close_music(*_args) -> None:
            close_order.append("music")

        async def close_location(*_args) -> None:
            close_order.append("location")

        with patch.object(
            douyin_commerce_session.douyin_music_service,
            "close_favorite_music_choices",
            new_callable=AsyncMock,
            side_effect=close_music,
        ) as close_music_picker, patch.object(
            douyin_commerce_session.douyin_commerce_service,
            "close_commerce_store_selector",
            new_callable=AsyncMock,
            side_effect=close_location,
        ) as close_location_selector:
            result = manager.prepare_publish_settings("session-demo")

        self.assertEqual(
            result,
            {"status": "clean", "sessionId": "session-demo", "openLayerCount": 0},
        )
        self.assertEqual(close_order, ["music", "location"])
        close_music_picker.assert_awaited_once_with(picker_page, dialog)
        close_location_selector.assert_awaited_once_with(page)
        self.assertIsNone(session.music_picker_page)
        self.assertIsNone(session.music_dialog)
        self.assertEqual(session.music_candidates, [])
        self.assertIsNone(session.selected_music)
        self.assertEqual(session.commerce_location_candidates, [])
        self.assertIsNone(session.location)
        self.assertEqual(session.location_scope, "")
        self.assertEqual(session.selected_declaration, "")
        self.assertEqual(session.stores, [])
        self.assertIsNone(session.selected_store)
        self.assertEqual(session.preflight_fingerprint, "")
        self.assertEqual(session.schedule_time, "")
        self.assertEqual(session.stage, "uploaded")

    def test_prepare_publish_settings_stops_when_music_layer_cannot_close(self) -> None:
        """音乐浮层无法唯一清理时必须精确归因，且不能继续清状态。"""

        class OpenPage:
            def is_closed(self) -> bool:
                return False

        manager = douyin_commerce_session.DouyinCommerceSessionManager()
        page = OpenPage()
        old_music = {"musicId": "music-old", "title": "旧音乐"}
        session = douyin_commerce_session._CommerceEditorSession(
            session_id="session-demo",
            upload_payload={},
            account_name="测试账号",
            browser=None,
            context=None,
            page=page,
            playwright=None,
            uploader=None,
            music_picker_page=object(),
            music_dialog=object(),
            music_candidates=[dict(old_music)],
            selected_music=dict(old_music),
        )
        manager._session = session
        with patch.object(
            douyin_commerce_session.douyin_music_service,
            "close_favorite_music_choices",
            new_callable=AsyncMock,
            side_effect=douyin_music_service.DouyinMusicError("关闭控件不唯一"),
        ), patch.object(
            douyin_commerce_session.douyin_commerce_service,
            "close_commerce_store_selector",
            new_callable=AsyncMock,
        ) as close_location_selector:
            with self.assertRaises(
                douyin_commerce_session.DouyinCommerceSessionError
            ) as raised:
                manager.prepare_publish_settings("session-demo")

        self.assertEqual(str(raised.exception), "正式发布页旧浮层未能清理")
        close_location_selector.assert_not_awaited()
        self.assertEqual(session.music_candidates, [old_music])
        self.assertEqual(session.selected_music, old_music)

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

class DouyinCommerceBatchUiTests(unittest.TestCase):
    """批量工作台仅验证本地控件，不触发浏览器或平台。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.page = DouyinCommercePage()

    def tearDown(self) -> None:
        self.page.close()

    class _InlineRunner:
        """让页面的后台任务在离线 UI 测试中同步完成。"""

        def is_running(self, _key: str) -> bool:
            return False

        def run(
            self,
            _key: str,
            *,
            with_progress,
            on_progress=None,
            on_success=None,
            on_error=None,
            on_finished=None,
        ) -> bool:
            try:
                result = with_progress(lambda event: on_progress(event) if on_progress else None)
                if on_success:
                    on_success(result)
            except Exception as exc:
                if on_error:
                    on_error(str(exc))
                else:
                    raise
            finally:
                if on_finished:
                    on_finished()
            return True

    class _ControlledLifecycleRunner:
        """确定性复现真实 runner：回调期间 active，finished 前先移除 key。"""

        def __init__(self) -> None:
            self.active: dict[str, dict[str, object]] = {}

        def is_running(self, key: str) -> bool:
            return key in self.active

        def run(
            self,
            key: str,
            fn=None,
            *,
            with_progress=None,
            on_started=None,
            on_progress=None,
            on_success=None,
            on_error=None,
            on_finished=None,
        ) -> bool:
            if key in self.active:
                return False
            if with_progress is None:
                if fn is None:
                    raise ValueError("测试任务必须提供执行函数")
                with_progress = lambda _report: fn()
            self.active[key] = {
                "worker": with_progress,
                "on_started": on_started,
                "on_progress": on_progress,
                "on_success": on_success,
                "on_error": on_error,
                "on_finished": on_finished,
            }
            return True

        def execute(self, key: str) -> None:
            job = self.active[key]
            on_started = job["on_started"]
            if callable(on_started):
                on_started()
            on_progress = job["on_progress"]
            report = lambda event: on_progress(event) if callable(on_progress) else None
            try:
                result = job["worker"](report)
            except Exception as exc:
                on_error = job["on_error"]
                if callable(on_error):
                    on_error(str(exc))
                else:
                    raise
            else:
                on_success = job["on_success"]
                if callable(on_success):
                    on_success(result)

        def finish(self, key: str) -> None:
            job = self.active.pop(key)
            on_finished = job["on_finished"]
            if callable(on_finished):
                on_finished()

    @staticmethod
    def _collector_status(
        *,
        domestic: str = "active",
        music: str = "not_started",
        local: str = "not_started",
        error_code: str = "",
    ) -> dict:
        """构造不含底层会话标识的完整 UI 采集状态。"""

        status = {
            "setupGenerationId": "generation-a",
            "generationState": "collecting",
            "collectors": {
                "domestic_location": domestic,
                "favorite_music": music,
                "local_location": local,
            },
            "collectorInstanceIds": {
                "domestic_location": "domestic-a" if domestic != "not_started" else "",
                "favorite_music": "music-a" if music != "not_started" else "",
                "local_location": "local-a" if local != "not_started" else "",
            },
        }
        if local != "not_started" or error_code:
            status["collectorDetails"] = {
                "local_location": {
                    "state": local,
                    "candidateCount": 0,
                    "durationMs": 37,
                    "errorCode": error_code,
                }
            }
        return status

    def test_platform_collector_progress_is_indeterminate_and_shows_elapsed_seconds(self) -> None:
        """平台采集只能显示真实等待时间，不得伪造完成百分比。"""

        with patch(
            "ui.douyin_commerce_page.time.monotonic",
            side_effect=[100.0, 100.0, 107.4],
        ):
            self.page._start_platform_collector_progress(
                "generation-a",
                "favorite_music",
                1,
                "正在刷新收藏音乐",
            )
            self.page._update_platform_collector_progress()

        self.assertFalse(self.page.platform_collector_progress_frame.isHidden())
        self.assertEqual(self.page.platform_collector_progress.minimum(), 0)
        self.assertEqual(self.page.platform_collector_progress.maximum(), 0)
        self.assertEqual(
            self.page.platform_collector_progress_label.text(),
            "正在刷新收藏音乐 · 已等待 7 秒",
        )

    def test_stale_collector_finish_cannot_hide_current_progress(self) -> None:
        """旧代际或旧 action token 的 finished 不得收口当前进度。"""

        self.page._start_platform_collector_progress(
            "generation-new",
            "local_location",
            3,
            "正在搜索本地地点",
        )

        self.page._finish_platform_collector_progress(
            "generation-old",
            "local_location",
            2,
        )

        self.assertFalse(self.page.platform_collector_progress_frame.isHidden())
        self.assertEqual(
            self.page._platform_collector_progress_owner,
            ("generation-new", "local_location", 3),
        )

    def test_collector_entry_points_show_fixed_progress_labels(self) -> None:
        """音乐、国内/本地搜索和受控重试必须各自显示固定动作。"""

        runner = self._ControlledLifecycleRunner()
        self.page.runner = runner
        self.page._setup_generation_id = "generation-a"

        self.page._refresh_favorite_music_candidates()
        self.assertTrue(
            self.page.platform_collector_progress_label.text().startswith(
                "正在刷新收藏音乐"
            )
        )
        runner.finish(self.page._COLLECTOR_TASK_KEY)

        self.page._search_batch_locations("domestic", "夜南香")
        self.assertTrue(
            self.page.platform_collector_progress_label.text().startswith(
                "正在搜索国内地点"
            )
        )
        runner.finish(self.page._COLLECTOR_TASK_KEY)

        self.page._search_batch_locations("local", "夜南香")
        self.assertTrue(
            self.page.platform_collector_progress_label.text().startswith(
                "正在搜索本地地点"
            )
        )
        runner.finish(self.page._COLLECTOR_TASK_KEY)

        self.page._last_failed_collector_type = "local_location"
        self.page._retry_last_failed_collector()
        self.assertTrue(
            self.page.platform_collector_progress_label.text().startswith(
                "正在重试本地地点"
            )
        )
        runner.finish(self.page._COLLECTOR_TASK_KEY)

        self.assertTrue(self.page.platform_collector_progress_frame.isHidden())

    def test_abandon_reset_stops_and_hides_platform_collector_progress(self) -> None:
        """放弃或代际重置时必须停止计时器并清除进度所有者。"""

        self.page._start_platform_collector_progress(
            "generation-a",
            "domestic_location",
            1,
            "正在准备国内地点采集",
        )

        self.page._reset_platform_settings_after_abandon()

        self.assertIsNone(self.page._platform_collector_progress_owner)
        self.assertFalse(self.page._platform_collector_progress_timer.isActive())
        self.assertTrue(self.page.platform_collector_progress_frame.isHidden())
        self.assertEqual(self.page.platform_collector_progress_label.text(), "")

    def test_setup_generation_progress_stays_visible_until_runner_finished(self) -> None:
        """国内初始采集在 worker 运行期间持续显示，不得被 success 提前隐藏。"""

        runner = self._ControlledLifecycleRunner()
        self.page.runner = runner
        with patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.begin_generation",
            return_value=self._collector_status(),
        ), patch.object(self.page, "_go_to_step"):
            self.page._start_setup_generation({"accountId": 31})
            self.assertTrue(
                self.page.platform_collector_progress_label.text().startswith(
                    "正在准备国内地点采集"
                )
            )
            runner.execute(self.page._SETUP_GENERATION_TASK_KEY)
            self.assertFalse(
                self.page.platform_collector_progress_frame.isHidden()
            )
            runner.finish(self.page._SETUP_GENERATION_TASK_KEY)

        self.assertTrue(self.page.platform_collector_progress_frame.isHidden())

    def test_login_required_resets_platform_collector_progress(self) -> None:
        """登录失效时不得留下仍在计时的旧采集动作。"""

        self.page._start_platform_collector_progress(
            "generation-a",
            "favorite_music",
            1,
            "正在刷新收藏音乐",
        )

        self.page._handle_login_required()

        self.assertIsNone(self.page._platform_collector_progress_owner)
        self.assertFalse(self.page._platform_collector_progress_timer.isActive())
        self.assertTrue(self.page.platform_collector_progress_frame.isHidden())

    def test_successful_generation_close_resets_platform_collector_progress(self) -> None:
        """采集代际严格归零后必须收口任何未完成进度。"""

        self.page._start_platform_collector_progress(
            "generation-a",
            "local_location",
            1,
            "正在搜索本地地点",
        )
        self.page._setup_close_token = 8

        self.page._setup_generation_close_succeeded(
            8,
            "preflight_started",
            True,
            {"closed": True, "aliveCollectorCount": 0},
        )

        self.assertIsNone(self.page._platform_collector_progress_owner)
        self.assertTrue(self.page.platform_collector_progress_frame.isHidden())

    def test_shutdown_resets_platform_collector_progress(self) -> None:
        """客户端退出路径必须停止 Qt 计时器，不得让其跨越窗口生命周期。"""

        self.page._start_platform_collector_progress(
            "generation-a",
            "domestic_location",
            1,
            "正在搜索国内地点",
        )

        self.assertTrue(self.page.shutdown())

        self.assertIsNone(self.page._platform_collector_progress_owner)
        self.assertFalse(self.page._platform_collector_progress_timer.isActive())
        self.assertTrue(self.page.platform_collector_progress_frame.isHidden())

    def test_completed_batch_clears_platform_collector_progress(self) -> None:
        """整批明确完成并清除选择时，不得保留上一批的进度所有者。"""

        self.page._start_platform_collector_progress(
            "generation-a",
            "favorite_music",
            1,
            "正在刷新收藏音乐",
        )

        self.page._clear_current_batch_platform_choices()

        self.assertIsNone(self.page._platform_collector_progress_owner)
        self.assertTrue(self.page.platform_collector_progress_frame.isHidden())

    def test_enter_platform_settings_starts_isolated_generation_without_publish_upload(self) -> None:
        """进入设置页只建立采集代际，不能把用户视频变成共享发布会话。"""

        self.page._selected_video_indexes = [1, 2]
        payload = {
            "accountList": ["douyin-setup.json"],
            "fileList": ["/tmp/user-first.mp4"],
            "title": "批量共享标题",
            "description": "批量共享文案",
            "tags": [],
        }
        self.page.runner = self._InlineRunner()
        with patch.object(self.page, "_batch_content_is_valid", return_value=True), patch.object(
            self.page, "collect_upload_payload", return_value=payload
        ), patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.begin_generation",
            return_value=self._collector_status(),
        ) as begin, patch(
            "ui.douyin_commerce_page.douyin_commerce_session.commerce_session_manager.start_upload"
        ) as legacy_upload:
            self.page.continue_after_content()

        begin.assert_called_once()
        legacy_upload.assert_not_called()
        self.assertEqual(self.page._setup_generation_id, "generation-a")
        self.assertEqual(self.page._session_id, "")
        self.assertEqual(self.page.pages.currentIndex(), 1)
        self.assertEqual(self.page.domestic_collector_status.text(), "国内地点：可用")
        self.assertEqual(self.page.music_collector_status.text(), "收藏音乐：点击刷新后启动")
        self.assertEqual(self.page.local_collector_status.text(), "本地点：首次搜索时启动")

    def test_music_location_and_declaration_are_staged_without_legacy_platform_writes(self) -> None:
        """采集页选择只更新本批公开字段，最终执行器才重新核验并写入。"""

        self.page._selected_video_indexes = [1]
        self.page._setup_generation_id = "generation-a"
        music = {
            "musicId": "music-1",
            "title": "收藏歌",
            "creator": "作者",
            "duration": "00:30",
            "privateMarker": "不得进入本地载荷",
        }
        location = {
            "poiId": "poi-1",
            "name": "夜南香北京烤鸭",
            "address": "陕西省安康市汉滨区江北办富民街2号",
            "distance": "2km",
            "privateMarker": "不得进入本地载荷",
        }

        with patch.object(
            douyin_commerce_session.commerce_session_manager,
            "select_favorite_music",
        ) as select_music, patch.object(
            douyin_commerce_session.commerce_session_manager,
            "select_cached_favorite_music",
        ) as select_cached, patch.object(
            douyin_commerce_session.commerce_session_manager,
            "apply_location",
        ) as apply_location, patch.object(
            douyin_commerce_session.commerce_session_manager,
            "select_content_declaration",
        ) as select_declaration:
            self.page._stage_music_selection(music)
            self.page._stage_location_selection(location)
            self.page._stage_declaration_selection("内容由AI生成")

        select_music.assert_not_called()
        select_cached.assert_not_called()
        apply_location.assert_not_called()
        select_declaration.assert_not_called()
        self.assertNotIn("privateMarker", self.page._selected_music)
        self.assertNotIn("privateMarker", self.page._selected_location_data)
        self.assertEqual(self.page._selected_location_data["locationScope"], "domestic")
        self.assertEqual(self.page._confirmed_declaration, "内容由AI生成")
        self.assertTrue(self.page._staged_music_confirmed)
        self.assertTrue(self.page._staged_location_confirmed)
        self.assertTrue(self.page._staged_declaration_confirmed)
        self.assertIn("发布时重新核验", self.page.music_status.text())

    def test_failed_local_collector_has_one_retry_slot_and_rejects_stale_result(self) -> None:
        """只重试失败采集器；旧代际回调不得覆盖当前 UI。"""

        self.page.runner = self._InlineRunner()
        self.page._setup_generation_id = "generation-a"
        self.page._selected_music = {"musicId": "music-1", "title": "收藏歌"}
        self.page._batch_location_searches["__shared_location_search__"] = {
            "scope": "domestic",
            "keyword": "夜南香",
            "candidates": [{"poiId": "poi-domestic", "name": "国内候选", "address": "完整地址"}],
        }
        failed = self._collector_status(local="failed", error_code="candidate_empty")
        self.page._render_collector_status(failed)

        retried = self._collector_status(local="active")
        retried.update(
            {
                "ok": True,
                "collectorType": "local_location",
                "collectorInstanceId": "local-a",
            }
        )
        with patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.retry_collector",
            return_value=retried,
        ) as retry, patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.status",
            return_value=retried,
        ):
            self.page._retry_last_failed_collector()

        retry.assert_called_once_with("generation-a", "local_location")
        self.assertEqual(self.page._selected_music["musicId"], "music-1")
        self.assertEqual(
            self.page._batch_location_searches["__shared_location_search__"]["candidates"][0]["poiId"],
            "poi-domestic",
        )
        self.assertTrue(self.page.retry_collector_button.isHidden())

        self.page._setup_generation_id = "generation-b"
        self.page._setup_generation_succeeded(self._collector_status())
        self.assertEqual(self.page._setup_generation_id, "generation-b")

    def test_abandon_setup_generation_clears_only_current_batch_platform_choices(self) -> None:
        """放弃代际关闭采集器并清本批选择，但保留内容输入和本地缓存。"""

        self.page.runner = self._InlineRunner()
        self.page._setup_generation_id = "generation-a"
        self.page._selected_music = {"musicId": "music-1", "title": "收藏歌"}
        self.page._batch_locations = {"/tmp/a.mp4": {"poiId": "poi-1"}}
        self.page._confirmed_declaration = "内容由AI生成"
        self.page._staged_music_confirmed = True
        self.page._staged_location_confirmed = True
        self.page._staged_declaration_confirmed = True
        self.page.title_input.setText("仍需保留的标题")

        def close_current(generation_id: str, *, reason: str) -> dict:
            self.assertEqual(self.page._setup_generation_id, "generation-a")
            self.assertEqual(generation_id, "generation-a")
            self.assertEqual(reason, "user_abandon")
            return {
                "closed": True,
                "setupGenerationId": generation_id,
                "aliveCollectorCount": 0,
            }

        with patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.close_generation",
            side_effect=close_current,
        ) as close_generation, patch(
            "ui.douyin_commerce_page.douyin_commerce_draft_service.load_content_draft",
            return_value=None,
        ):
            self.page._abandon_session(silent=True)

        close_generation.assert_called_once_with("generation-a", reason="user_abandon")
        self.assertEqual(self.page._setup_generation_id, "")
        self.assertIsNone(self.page._selected_music)
        self.assertEqual(self.page._batch_locations, {})
        self.assertEqual(self.page._confirmed_declaration, "")
        self.assertFalse(self.page._staged_music_confirmed)
        self.assertFalse(self.page._staged_location_confirmed)
        self.assertFalse(self.page._staged_declaration_confirmed)
        self.assertEqual(self.page.title_input.text(), "仍需保留的标题")

    def test_login_required_closes_setup_generation_before_reset(self) -> None:
        """登录失效必须统一关闭当前代际，不得只清理 UI 句柄。"""

        self.page.runner = self._InlineRunner()
        self.page._setup_generation_id = "generation-a"
        self.page._selected_music = {"musicId": "music-1", "title": "收藏歌"}

        with patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.close_generation",
            return_value={"closed": True, "aliveCollectorCount": 0},
        ) as close_generation:
            self.page._handle_login_required()

        close_generation.assert_called_once_with(
            "generation-a", reason="login_required"
        )
        self.assertEqual(self.page._setup_generation_id, "")
        self.assertIsNone(self.page._selected_music)
        self.assertFalse(self.page.login_required_frame.isHidden())

    def test_new_setup_generation_closes_previous_generation_with_fixed_reason(self) -> None:
        """账号或内容变化创建新代际前，先以固定原因关闭旧代际。"""

        self.page.runner = self._InlineRunner()
        self.page._setup_generation_id = "generation-a"
        order: list[tuple[str, str]] = []
        started = self._collector_status()
        started["setupGenerationId"] = "generation-b"

        def close_generation(generation_id: str, *, reason: str) -> dict:
            order.append(("close", reason))
            self.assertEqual(generation_id, "generation-a")
            return {"closed": True, "aliveCollectorCount": 0}

        def begin_generation(_payload: dict, *, on_progress=None) -> dict:
            del on_progress
            order.append(("begin", "generation-b"))
            return started

        with patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.close_generation",
            side_effect=close_generation,
        ), patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.begin_generation",
            side_effect=begin_generation,
        ), patch.object(self.page, "_go_to_step"):
            self.page._start_setup_generation({"accountId": 31})

        self.assertEqual(
            order,
            [("close", "generation_replaced"), ("begin", "generation-b")],
        )
        self.assertEqual(self.page._setup_generation_id, "generation-b")

    def test_failed_generation_start_closes_partial_generation(self) -> None:
        """代际已有公开 ID 但国内采集器未就绪时，必须收口故障会话。"""

        self.page.runner = self._InlineRunner()
        failed = self._collector_status(domestic="failed")
        with patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.close_generation",
            return_value={"closed": True, "aliveCollectorCount": 0},
        ) as close_generation:
            self.page._setup_generation_succeeded(failed)

        close_generation.assert_called_once_with(
            "generation-a", reason="operation_failed"
        )
        self.assertEqual(self.page._setup_generation_id, "")
        self.assertIn("collector_start_failed", self.page.platform_review_status.text())

    def test_generation_start_exception_closes_runtime_without_public_id(self) -> None:
        """启动内部已建 runtime 却未返回 ID 时，异常路径仍须统一取消关闭。"""

        self.page.runner = self._InlineRunner()
        with patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.begin_generation",
            side_effect=RuntimeError("collector_start_failed"),
        ), patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.close_generation",
            return_value={"closed": True, "aliveCollectorCount": 0},
        ) as close_generation:
            self.page._start_setup_generation({"accountId": 31})

        close_generation.assert_called_once_with(None, reason="operation_failed")
        self.assertEqual(self.page._setup_generation_id, "")
        self.assertIn("collector_start_failed", self.page.platform_review_status.text())

    def test_close_setup_generation_without_active_generation_is_already_closed(self) -> None:
        """无活动代际时关闭是幂等成功，不应调用平台协调器。"""

        with patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.close_generation"
        ) as close_generation:
            result = self.page._close_setup_generation("user_abandon")

        close_generation.assert_not_called()
        self.assertEqual(result["closed"], True)
        self.assertEqual(result["aliveCollectorCount"], 0)

    def test_shutdown_closes_collectors_and_legacy_publish_session_without_dialog(self) -> None:
        """客户端退出时必须尽力关闭代际和旧正式会话，且不弹窗。"""

        self.page._setup_generation_id = "generation-a"
        self.page._session_id = "session-legacy"
        with patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.close_generation",
            return_value={"closed": True, "aliveCollectorCount": 0},
        ) as close_generation, patch.object(
            douyin_commerce_session.commerce_session_manager, "close"
        ) as close_session, patch(
            "ui.douyin_commerce_page.QMessageBox.information"
        ) as information, patch(
            "ui.douyin_commerce_page.QMessageBox.warning"
        ) as warning:
            shutdown_succeeded = self.page.shutdown()

        close_generation.assert_called_once_with(
            "generation-a", reason="client_shutdown"
        )
        close_session.assert_called_once_with("session-legacy")
        self.assertEqual(self.page._setup_generation_id, "")
        self.assertEqual(self.page._session_id, "")
        self.assertIs(shutdown_succeeded, True)
        information.assert_not_called()
        warning.assert_not_called()

    def test_shutdown_cancels_queued_setup_before_worker_can_begin_generation(self) -> None:
        """已入线程池但尚未执行的 setup 必须在退出返回前不可逆取消。"""

        class QueuedPool:
            def __init__(self) -> None:
                self.tasks: list[BackgroundTask] = []

            def start(self, task: BackgroundTask) -> None:
                self.tasks.append(task)

        queued_pool = QueuedPool()
        runner = BackgroundTaskRunner(self.page)
        runner.pool = queued_pool
        self.page.runner = runner
        started = self._collector_status()

        with patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.begin_generation",
            return_value=started,
        ) as begin_generation, patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.close_generation",
            return_value={
                "closed": True,
                "setupGenerationId": "",
                "aliveCollectorCount": 0,
            },
        ), patch("ui.douyin_commerce_page.QMessageBox.warning"):
            self.page._start_setup_generation({"accountId": 31})
            self.assertEqual(len(queued_pool.tasks), 1)

            shutdown_succeeded = self.page.shutdown()
            queued_pool.tasks[0].run()
            QApplication.processEvents()

        begin_generation.assert_not_called()
        self.assertIs(shutdown_succeeded, True)
        self.assertFalse(runner.is_running(self.page._SETUP_GENERATION_TASK_KEY))
        self.assertEqual(self.page._setup_generation_id, "")

    def test_shutdown_reports_failure_when_running_setup_misses_bounded_deadline(self) -> None:
        """运行中的 setup 未在截止内收束时，退出必须返回失败而非假报零存活。"""

        class TimedOutRunner:
            def is_running(self, key: str) -> bool:
                return key == self.page._SETUP_GENERATION_TASK_KEY

            def cancel_pending(self, _key: str) -> bool:
                return False

            def wait_for_finished(self, _key: str, _timeout_seconds: float) -> bool:
                return False

        runner = TimedOutRunner()
        runner.page = self.page
        self.page.runner = runner
        with patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.close_generation",
            return_value={"closed": True, "aliveCollectorCount": 0},
        ):
            shutdown_succeeded = self.page.shutdown()

        self.assertIs(shutdown_succeeded, False)

    def test_timed_out_setup_closes_generation_when_worker_returns_late(self) -> None:
        """退出已被阻止后，迟到的 begin 结果仍须在 worker 内 client_shutdown。"""

        class RunningPool:
            def __init__(self) -> None:
                self.thread: threading.Thread | None = None

            def start(self, task: BackgroundTask) -> None:
                self.thread = threading.Thread(target=task.run, daemon=True)
                self.thread.start()

        begin_entered = threading.Event()
        allow_return = threading.Event()
        running_pool = RunningPool()
        runner = BackgroundTaskRunner(self.page)
        runner.pool = running_pool
        self.page.runner = runner
        self.page._SHUTDOWN_WAIT_SECONDS = 0.001
        started = self._collector_status()

        def blocked_begin(_payload: dict, *, on_progress=None) -> dict:
            del on_progress
            begin_entered.set()
            if not allow_return.wait(2):
                raise AssertionError("测试未释放 begin_generation")
            return started

        with patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.begin_generation",
            side_effect=blocked_begin,
        ), patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.close_generation",
            return_value={
                "closed": True,
                "setupGenerationId": "generation-a",
                "aliveCollectorCount": 0,
            },
        ) as close_generation:
            self.page._start_setup_generation({"accountId": 31})
            self.assertTrue(begin_entered.wait(1))
            shutdown_succeeded = self.page.shutdown()
            try:
                self.assertIs(shutdown_succeeded, False)
            finally:
                allow_return.set()
                self.assertIsNotNone(running_pool.thread)
                running_pool.thread.join(2)
                QApplication.processEvents()

        close_generation.assert_called_with(
            "generation-a", reason="client_shutdown"
        )
        self.assertFalse(self.page._setup_generation_cleanup_required)
        self.assertEqual(self.page._setup_generation_id, "")

    def test_collector_barrier_rejects_non_strict_zero_alive_proofs(self) -> None:
        """缺字段或可强转为零的值都不是全部采集器已关闭的证据。"""

        invalid_results = (
            [],
            {"closed": True},
            {"closed": True, "aliveCollectorCount": False},
            {"closed": True, "aliveCollectorCount": 0.0},
            {"closed": True, "aliveCollectorCount": "0"},
            {"closed": False, "aliveCollectorCount": 0},
        )
        for close_result in invalid_results:
            with self.subTest(close_result=close_result):
                self.page._setup_generation_id = "generation-a"
                operation = MagicMock()
                with patch(
                    "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.close_generation",
                    return_value=close_result,
                ), self.assertRaisesRegex(
                    RuntimeError,
                    "平台设置临时会话未完全关闭，已安全停止",
                ):
                    self.page._run_after_collector_barrier(
                        operation,
                        reason="preflight_started",
                    )
                operation.assert_not_called()

    def test_operation_failed_close_refreshes_only_after_runner_removes_active_key(self) -> None:
        """异常关闭的 finished 刷新必须发生在 key 删除后，恢复被 busy 禁用的控件。"""

        runner = self._ControlledLifecycleRunner()
        self.page.runner = runner
        failed = self._collector_status(domestic="failed")
        with patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.begin_generation",
            return_value=failed,
        ), patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.close_generation",
            return_value={"closed": True, "aliveCollectorCount": 0},
        ):
            self.page._start_setup_generation({"accountId": 31})
            runner.execute(self.page._SETUP_GENERATION_TASK_KEY)
            runner.finish(self.page._SETUP_GENERATION_TASK_KEY)
            runner.execute(self.page._SETUP_GENERATION_CLOSE_TASK_KEY)

            self.assertTrue(self.page._busy())
            self.assertFalse(self.page.save_content_button.isEnabled())
            runner.finish(self.page._SETUP_GENERATION_CLOSE_TASK_KEY)

        self.assertFalse(self.page._busy())
        self.assertTrue(self.page.save_content_button.isEnabled())

    @staticmethod
    def _create_offline_pending_task(*, mode: str) -> dict:
        return task_service.create_pending_task(
            [
                {
                    "type": 3,
                    "accountList": ["oneclick_3_offline.json"],
                    "fileList": ["/tmp/offline-video.mp4"],
                    "contentType": "video",
                    "title": "关闭屏障离线测试",
                    "debugDryRun": mode == "oneclick_preflight",
                }
            ],
            mode=mode,
        )

    def test_preflight_barrier_failure_keeps_ledger_non_running_until_finished(self) -> None:
        """预检屏障失败时台账不得 running，且 finished 前保留 taskId 和固定原因。"""

        with tempfile.TemporaryDirectory() as directory, patch.object(
            database, "DB_PATH", Path(directory) / "database.db"
        ):
            database.ensure_schema()
            task = self._create_offline_pending_task(mode="oneclick_preflight")
            runner = self._ControlledLifecycleRunner()
            self.page.runner = runner
            self.page._setup_generation_id = "generation-a"
            self.page._selected_video_indexes = [1]
            payload = {"items": [{"mediaPath": "/tmp/offline-video.mp4"}]}
            with patch.object(
                self.page, "collect_batch_payload", return_value=payload
            ), patch(
                "ui.douyin_commerce_page.task_service.create_douyin_batch_task",
                return_value=task,
            ), patch(
                "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.close_generation",
                return_value={"closed": False, "aliveCollectorCount": 1},
            ), patch.object(
                self.page._batch_executor, "run_preflight"
            ) as run_preflight, patch(
                "ui.douyin_commerce_page.QMessageBox.warning"
            ):
                self.page.start_batch_preflight()
                runner.execute("douyin_commerce_batch_run")
                try:
                    stored = task_service.get_task(task["id"])
                    self.assertIsNotNone(stored)
                    self.assertNotEqual(stored["status"], "running")
                    self.assertEqual(self.page._batch_task_id, task["id"])
                    self.assertEqual(
                        self.page._batch_result_feedback,
                        "批量任务未完成：平台设置临时会话未完全关闭，已安全停止",
                    )
                    run_preflight.assert_not_called()
                finally:
                    runner.finish("douyin_commerce_batch_run")
            self.assertIsNone(self.page._batch_task_id)

    def test_publish_barrier_failure_keeps_ledger_non_running_until_finished(self) -> None:
        """发布屏障失败时台账不得 running，且 executor 未启动、固定归因仍可见。"""

        with tempfile.TemporaryDirectory() as directory, patch.object(
            database, "DB_PATH", Path(directory) / "database.db"
        ):
            database.ensure_schema()
            task = self._create_offline_pending_task(mode="oneclick_publish")
            runner = self._ControlledLifecycleRunner()
            self.page.runner = runner
            self.page._setup_generation_id = "generation-a"
            self.page._selected_video_indexes = [1]
            payload = {"items": [{"mediaPath": "/tmp/offline-video.mp4"}]}
            with patch(
                "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.close_generation",
                return_value={"closed": False, "aliveCollectorCount": 1},
            ), patch.object(
                self.page._batch_executor, "run_publish"
            ) as run_publish, patch.object(
                self.page, "_start_douyin_verification_polling"
            ), patch(
                "ui.douyin_commerce_page.QMessageBox.warning"
            ):
                self.page.start_batch_publish(payload, task)
                runner.execute("douyin_commerce_batch_run")
                try:
                    stored = task_service.get_task(task["id"])
                    self.assertIsNotNone(stored)
                    self.assertNotEqual(stored["status"], "running")
                    self.assertEqual(self.page._batch_task_id, task["id"])
                    self.assertEqual(
                        self.page._batch_result_feedback,
                        "批量任务未完成：平台设置临时会话未完全关闭，已安全停止",
                    )
                    run_publish.assert_not_called()
                finally:
                    runner.finish("douyin_commerce_batch_run")
            self.assertIsNone(self.page._batch_task_id)

    def test_batch_preflight_stops_before_executor_when_collector_remains_alive(self) -> None:
        """预检关闭屏障未取得零存活时，不得创建任何正式编辑会话。"""

        self.page.runner = self._InlineRunner()
        self.page._setup_generation_id = "generation-a"
        self.page._selected_video_indexes = [1]
        payload = {"items": [{"mediaPath": "/tmp/video-1.mp4"}]}
        with patch.object(
            self.page, "collect_batch_payload", return_value=payload
        ), patch(
            "ui.douyin_commerce_page.task_service.create_douyin_batch_task",
            return_value={"id": 71},
        ), patch(
            "ui.douyin_commerce_page.task_service.mark_task_running"
        ), patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.close_generation",
            return_value={"closed": False, "aliveCollectorCount": 1},
        ) as close_generation, patch.object(
            self.page._batch_executor, "run_preflight"
        ) as run_preflight, patch(
            "ui.douyin_commerce_page.QMessageBox.warning"
        ):
            self.page.start_batch_preflight()

        close_generation.assert_called_once_with(
            "generation-a", reason="preflight_started"
        )
        run_preflight.assert_not_called()
        self.assertEqual(self.page._setup_generation_id, "generation-a")
        self.assertIn(
            "平台设置临时会话未完全关闭，已安全停止",
            self.page.validation_label.text(),
        )

    def test_batch_publish_stops_before_executor_when_collector_remains_alive(self) -> None:
        """正式发布关闭屏障失败时，不得上传第一条视频。"""

        self.page.runner = self._InlineRunner()
        self.page._setup_generation_id = "generation-a"
        self.page._selected_video_indexes = [1]
        payload = {"items": [{"mediaPath": "/tmp/video-1.mp4"}]}
        with patch.object(
            self.page, "collect_batch_payload", return_value=payload
        ), patch(
            "ui.douyin_commerce_page.task_service.mark_task_running"
        ), patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.close_generation",
            return_value={"closed": False, "aliveCollectorCount": 1},
        ) as close_generation, patch.object(
            self.page._batch_executor, "run_publish"
        ) as run_publish, patch(
            "ui.douyin_commerce_page.QMessageBox.warning"
        ), patch(
            "ui.douyin_commerce_page.QMessageBox.information"
        ):
            self.page.start_batch_publish(payload, {"id": 72})

        close_generation.assert_called_once_with(
            "generation-a", reason="publish_started"
        )
        run_publish.assert_not_called()
        self.assertEqual(self.page._setup_generation_id, "generation-a")
        self.assertIn(
            "平台设置临时会话未完全关闭，已安全停止",
            self.page.validation_label.text(),
        )

    def test_batch_preflight_runs_only_after_zero_alive_barrier(self) -> None:
        """零存活回读必须先清除代际句柄，再进入执行器。"""

        self.page.runner = self._InlineRunner()
        self.page._setup_generation_id = "generation-a"
        self.page._selected_video_indexes = [1]
        payload = {"items": [{"mediaPath": "/tmp/video-1.mp4"}]}

        def run_preflight(*_args, **_kwargs):
            self.assertEqual(self.page._setup_generation_id, "")
            return [{"index": 0, "status": "preflighted"}]

        with patch.object(
            self.page, "collect_batch_payload", return_value=payload
        ), patch(
            "ui.douyin_commerce_page.task_service.create_douyin_batch_task",
            return_value={"id": 73},
        ), patch(
            "ui.douyin_commerce_page.task_service.mark_task_running"
        ), patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.close_generation",
            return_value={"closed": True, "aliveCollectorCount": 0},
        ), patch.object(
            self.page._batch_executor,
            "run_preflight",
            side_effect=run_preflight,
        ) as executor, patch(
            "ui.douyin_commerce_page.QMessageBox.warning"
        ):
            self.page.start_batch_preflight()

        executor.assert_called_once()
        self.assertEqual(self.page._setup_generation_id, "")

    def test_batch_publish_runs_only_after_zero_alive_barrier(self) -> None:
        """正式发布执行器也只能在零存活回读后启动。"""

        self.page.runner = self._InlineRunner()
        self.page._setup_generation_id = "generation-a"
        self.page._selected_video_indexes = [1]
        payload = {"items": [{"mediaPath": "/tmp/video-1.mp4"}]}

        def run_publish(*_args, **_kwargs):
            self.assertEqual(self.page._setup_generation_id, "")
            return [{"index": 0, "status": "failed", "diagnostic": "离线测试"}]

        with patch(
            "ui.douyin_commerce_page.task_service.mark_task_running"
        ), patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.close_generation",
            return_value={"closed": True, "aliveCollectorCount": 0},
        ), patch.object(
            self.page._batch_executor,
            "run_publish",
            side_effect=run_publish,
        ) as executor, patch(
            "ui.douyin_commerce_page.QMessageBox.warning"
        ):
            self.page.start_batch_publish(payload, {"id": 74})

        executor.assert_called_once()
        self.assertEqual(self.page._setup_generation_id, "")

    def test_abandon_cleanup_incomplete_retains_generation_and_choices(self) -> None:
        """放弃关闭不完整时保留安全重试所需句柄与本批选择。"""

        self.page.runner = self._InlineRunner()
        self.page._setup_generation_id = "generation-a"
        self.page._selected_music = {"musicId": "music-1", "title": "收藏歌"}
        self.page._batch_locations = {"/tmp/a.mp4": {"poiId": "poi-1"}}
        self.page._confirmed_declaration = "内容由AI生成"

        with patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.close_generation",
            return_value={
                "closed": False,
                "setupGenerationId": "generation-a",
                "aliveCollectorCount": 1,
            },
        ), patch(
            "ui.douyin_commerce_page.douyin_commerce_draft_service.load_content_draft",
            return_value=None,
        ):
            self.page._abandon_session(silent=True)

        self.assertEqual(self.page._setup_generation_id, "generation-a")
        self.assertEqual(self.page._selected_music["musicId"], "music-1")
        self.assertEqual(self.page._batch_locations["/tmp/a.mp4"]["poiId"], "poi-1")
        self.assertEqual(self.page._confirmed_declaration, "内容由AI生成")
        self.assertIn("cleanup_incomplete", self.page.platform_review_status.text())

    def test_abandon_generation_close_is_dispatched_without_blocking_ui(self) -> None:
        """放弃只派发后台关闭；调用返回前不得同步进入可能阻塞的 close。"""

        self.page._setup_generation_id = "generation-a"
        self.page._selected_music = {"musicId": "music-1", "title": "收藏歌"}
        self.page.runner = MagicMock()
        self.page.runner.is_running.return_value = False
        self.page.runner.run.return_value = True

        with patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.close_generation"
        ) as close_generation:
            self.page._abandon_session(silent=True)

        close_generation.assert_not_called()
        self.page.runner.run.assert_called_once()
        self.assertEqual(self.page._setup_generation_id, "generation-a")
        self.assertEqual(self.page._selected_music["musicId"], "music-1")

    def test_abandon_close_exception_uses_fixed_error_without_leaking_raw_text(self) -> None:
        """后台关闭异常只显示固定码，并保留后续安全重试所需状态。"""

        self.page.runner = self._InlineRunner()
        self.page._setup_generation_id = "generation-a"
        self.page._selected_music = {"musicId": "music-1", "title": "收藏歌"}

        with patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.close_generation",
            side_effect=RuntimeError("Cookie=secret DOM=<html>private</html>"),
        ):
            self.page._abandon_session(silent=True)

        visible = self.page.platform_review_status.text()
        self.assertEqual(self.page._setup_generation_id, "generation-a")
        self.assertEqual(self.page._selected_music["musicId"], "music-1")
        self.assertIn("cleanup_incomplete", visible)
        self.assertNotIn("secret", visible)
        self.assertNotIn("<html>", visible)

    def test_fully_published_batch_clears_current_platform_choices(self) -> None:
        """整批有明确发布回执后，当前批选择不得自动沿用到下一批。"""

        self.page.runner = self._InlineRunner()
        self.page._setup_generation_id = "generation-a"
        self.page._selected_music = {"musicId": "music-1", "title": "收藏歌"}
        self.page._batch_locations = {"/tmp/a.mp4": {"poiId": "poi-1"}}
        self.page._confirmed_declaration = "内容由AI生成"
        self.page._staged_music_confirmed = True
        self.page._staged_location_confirmed = True
        self.page._staged_declaration_confirmed = True

        with patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.close_generation",
            return_value={
                "closed": True,
                "setupGenerationId": "generation-a",
                "aliveCollectorCount": 0,
            },
        ) as close_generation, patch("ui.douyin_commerce_page.QMessageBox.information"):
            self.page._batch_publish_succeeded([{"index": 0, "status": "published"}])

        close_generation.assert_called_once_with(
            "generation-a", reason="publish_completed"
        )
        self.assertEqual(self.page._setup_generation_id, "")
        self.assertIsNone(self.page._selected_music)
        self.assertEqual(self.page._batch_locations, {})
        self.assertEqual(self.page._confirmed_declaration, "")
        self.assertFalse(self.page._staged_music_confirmed)
        self.assertFalse(self.page._staged_location_confirmed)
        self.assertFalse(self.page._staged_declaration_confirmed)

    def test_fully_published_batch_retains_generation_when_close_is_incomplete(self) -> None:
        """明确发布完成仍须等采集器全部关闭后才能撤销句柄和选择。"""

        self.page.runner = self._InlineRunner()
        self.page._setup_generation_id = "generation-a"
        self.page._selected_music = {"musicId": "music-1", "title": "收藏歌"}
        self.page._batch_locations = {"/tmp/a.mp4": {"poiId": "poi-1"}}
        self.page._confirmed_declaration = "内容由AI生成"

        with patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.close_generation",
            return_value={
                "closed": False,
                "setupGenerationId": "generation-a",
                "aliveCollectorCount": 1,
            },
        ), patch(
            "ui.douyin_commerce_page.QMessageBox.information"
        ):
            self.page._batch_publish_succeeded([{"index": 0, "status": "published"}])

        self.assertEqual(self.page._setup_generation_id, "generation-a")
        self.assertEqual(self.page._selected_music["musicId"], "music-1")
        self.assertEqual(self.page._batch_locations["/tmp/a.mp4"]["poiId"], "poi-1")
        self.assertEqual(self.page._confirmed_declaration, "内容由AI生成")
        self.assertIn("cleanup_incomplete", self.page.platform_review_status.text())

    def test_music_domestic_and_local_collectors_accept_any_order_in_one_generation(self) -> None:
        """三种操作顺序都复用同一代际，且不会清空已暂存的独立选择。"""

        candidate = {"musicId": "music-1", "title": "收藏歌", "creator": "作者", "duration": "00:30"}
        location = {"poiId": "poi-1", "name": "夜南香", "address": "陕西省安康市完整地址"}
        active = self._collector_status(music="active", local="active")
        orders = (
            ("music", "domestic", "local"),
            ("domestic", "music", "local"),
            ("local", "domestic", "music"),
        )

        for order in orders:
            with self.subTest(order=order):
                self.page.runner = self._InlineRunner()
                self.page._setup_generation_id = "generation-a"
                self.page._stage_declaration_selection("内容由AI生成")

                def music_result(_generation_id: str) -> dict:
                    return {
                        **active,
                        "ok": True,
                        "collectorType": "favorite_music",
                        "collectorInstanceId": "music-a",
                        "candidates": [candidate],
                    }

                def location_result(
                    _generation_id: str, _keyword: str, scope: str
                ) -> dict:
                    collector_type = f"{scope}_location"
                    return {
                        **active,
                        "ok": True,
                        "collectorType": collector_type,
                        "collectorInstanceId": active["collectorInstanceIds"][
                            collector_type
                        ],
                        "candidates": [location],
                    }

                with patch(
                    "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.refresh_favorite_music",
                    side_effect=music_result,
                ) as refresh_music, patch(
                    "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.search_locations",
                    side_effect=location_result,
                ) as search_locations, patch(
                    "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.status",
                    return_value=active,
                ):
                    for action in order:
                        if action == "music":
                            self.page._refresh_favorite_music_candidates()
                            self.page._stage_music_selection(candidate)
                        else:
                            self.page._search_batch_locations(action, "夜南香")

                if "music" in order:
                    refresh_music.assert_called_once_with("generation-a")
                self.assertEqual(
                    [item.args for item in search_locations.call_args_list],
                    [
                        ("generation-a", "夜南香", action)
                        for action in order
                        if action in {"domestic", "local"}
                    ],
                )
                self.assertEqual(self.page._selected_music["musicId"], "music-1")
                self.assertEqual(self.page._confirmed_declaration, "内容由AI生成")

    def test_late_collector_instance_callback_cannot_write_current_candidates(self) -> None:
        """同代际旧实例回调也必须丢弃，不能只检查 generationId。"""

        self.page._setup_generation_id = "generation-a"
        self.page._collector_action_tokens["favorite_music"] = 4
        accepted: list[object] = []
        current = self._collector_status(music="active")
        current["collectorInstanceIds"]["favorite_music"] = "music-new"
        stale = {
            "ok": True,
            "setupGenerationId": "generation-a",
            "collectorType": "favorite_music",
            "collectorInstanceId": "music-old",
            "candidates": [{"musicId": "stale"}],
            "collectors": {
                "domestic_location": "active",
                "favorite_music": "active",
                "local_location": "not_started",
            },
            "collectorInstanceIds": {
                "domestic_location": "domestic-a",
                "favorite_music": "music-old",
                "local_location": "",
            },
        }

        with patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.status",
            return_value=current,
        ):
            self.page._collector_action_succeeded(
                "generation-a", "favorite_music", 4, stale, accepted.append
            )

        self.assertEqual(accepted, [])
        self.assertEqual(self.page._music_candidates, [])

    def test_late_collector_instance_failure_cannot_replace_current_status(self) -> None:
        """旧实例失败回调不得把当前新实例标记为失败或暴露重试入口。"""

        self.page._setup_generation_id = "generation-a"
        self.page._collector_action_tokens["favorite_music"] = 4
        current = self._collector_status(music="active")
        current["collectorInstanceIds"]["favorite_music"] = "music-new"
        self.page._render_collector_status(current)
        stale_failure = {
            "ok": False,
            "errorCode": "collector_unknown",
            "setupGenerationId": "generation-a",
            "collectorType": "favorite_music",
            "collectorInstanceId": "music-old",
        }

        with patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.status",
            return_value=current,
        ):
            self.page._collector_action_failed(
                "generation-a", "favorite_music", 4, stale_failure
            )

        self.assertEqual(self.page.music_collector_status.text(), "收藏音乐：可用")
        self.assertTrue(self.page.retry_collector_button.isHidden())

    def test_current_music_login_required_routes_to_account_management(self) -> None:
        """收藏音乐懒启动的当前实例登录失效必须进入账号管理。"""

        self.page._setup_generation_id = "generation-a"
        self.page._collector_action_tokens["favorite_music"] = 4
        current = self._collector_status(music="active")
        self.page._render_collector_status(current)
        login_required = {
            "ok": False,
            "errorCode": "login_required",
            "setupGenerationId": "generation-a",
            "collectorType": "favorite_music",
            "collectorInstanceId": "music-a",
        }

        with patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.status",
            return_value=current,
        ), patch.object(self.page, "_handle_login_required") as handle_login:
            self.page._collector_action_failed(
                "generation-a", "favorite_music", 4, login_required
            )

        handle_login.assert_called_once_with()
        self.assertEqual(self.page.music_collector_status.text(), "收藏音乐：可用")
        self.assertTrue(self.page.retry_collector_button.isHidden())

    def test_current_local_login_required_routes_to_account_management(self) -> None:
        """本地点懒启动的当前实例登录失效必须进入账号管理。"""

        self.page._setup_generation_id = "generation-a"
        self.page._collector_action_tokens["local_location"] = 7
        current = self._collector_status(local="active")
        current["generationState"] = "ready"
        self.page._render_collector_status(current)
        login_required = {
            "ok": False,
            "errorCode": "login_required",
            "setupGenerationId": "generation-a",
            "collectorType": "local_location",
            "collectorInstanceId": "local-a",
        }

        with patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.status",
            return_value=current,
        ), patch.object(self.page, "_handle_login_required") as handle_login:
            self.page._collector_action_failed(
                "generation-a", "local_location", 7, login_required
            )

        handle_login.assert_called_once_with()
        self.assertIn("本地点：可用", self.page.local_collector_status.text())
        self.assertNotIn("失败", self.page.local_collector_status.text())
        self.assertTrue(self.page.retry_collector_button.isHidden())

    def test_late_music_login_required_does_not_route_to_account_management(self) -> None:
        """旧音乐实例的迟到登录错误不得打断当前新实例。"""

        self.page._setup_generation_id = "generation-a"
        self.page._collector_action_tokens["favorite_music"] = 4
        current = self._collector_status(music="active")
        current["collectorInstanceIds"]["favorite_music"] = "music-new"
        self.page._render_collector_status(current)
        stale_login = {
            "ok": False,
            "errorCode": "login_required",
            "setupGenerationId": "generation-a",
            "collectorType": "favorite_music",
            "collectorInstanceId": "music-old",
        }

        with patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.status",
            return_value=current,
        ), patch.object(self.page, "_handle_login_required") as handle_login:
            self.page._collector_action_failed(
                "generation-a", "favorite_music", 4, stale_login
            )

        handle_login.assert_not_called()
        self.assertEqual(self.page.music_collector_status.text(), "收藏音乐：可用")
        self.assertTrue(self.page.retry_collector_button.isHidden())

    def test_music_login_required_during_cancelling_generation_is_discarded(self) -> None:
        """音乐登录错误到达时若代际已取消，不得清选择或切到账号管理。"""

        self.page._setup_generation_id = "generation-a"
        self.page._collector_action_tokens["favorite_music"] = 4
        self.page._selected_music = {"musicId": "music-1", "title": "收藏歌"}
        self.page._batch_locations = {"/tmp/a.mp4": {"poiId": "poi-1"}}
        self.page.pages.setCurrentIndex(1)
        closing = self._collector_status(music="active")
        closing["generationState"] = "cancelling"
        login_required = {
            "ok": False,
            "errorCode": "login_required",
            "setupGenerationId": "generation-a",
            "collectorType": "favorite_music",
            "collectorInstanceId": "music-a",
        }

        with patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.status",
            return_value=closing,
        ), patch.object(self.page, "_handle_login_required") as handle_login:
            self.page._collector_action_failed(
                "generation-a", "favorite_music", 4, login_required
            )

        handle_login.assert_not_called()
        self.assertEqual(self.page._selected_music["musicId"], "music-1")
        self.assertEqual(self.page._batch_locations["/tmp/a.mp4"]["poiId"], "poi-1")
        self.assertEqual(self.page.pages.currentIndex(), 1)

    def test_local_login_required_during_closing_collectors_is_discarded(self) -> None:
        """本地点登录错误到达时若采集器正在关闭，不得清选择或切页。"""

        self.page._setup_generation_id = "generation-a"
        self.page._collector_action_tokens["local_location"] = 7
        self.page._selected_music = {"musicId": "music-1", "title": "收藏歌"}
        self.page._batch_locations = {"/tmp/a.mp4": {"poiId": "poi-1"}}
        self.page.pages.setCurrentIndex(1)
        closing = self._collector_status(local="active")
        closing["generationState"] = "closing_collectors"
        login_required = {
            "ok": False,
            "errorCode": "login_required",
            "setupGenerationId": "generation-a",
            "collectorType": "local_location",
            "collectorInstanceId": "local-a",
        }

        with patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.status",
            return_value=closing,
        ), patch.object(self.page, "_handle_login_required") as handle_login:
            self.page._collector_action_failed(
                "generation-a", "local_location", 7, login_required
            )

        handle_login.assert_not_called()
        self.assertEqual(self.page._selected_music["musicId"], "music-1")
        self.assertEqual(self.page._batch_locations["/tmp/a.mp4"]["poiId"], "poi-1")
        self.assertEqual(self.page.pages.currentIndex(), 1)

    def test_retry_cleanup_failure_for_current_old_instance_is_visible(self) -> None:
        """retry 旧实例关闭失败应显示固定失败并保留单槽重试入口。"""

        self.page._setup_generation_id = "generation-a"
        self.page._collector_action_tokens["domestic_location"] = 3
        current = self._collector_status(domestic="failed")
        self.page._render_collector_status(current)
        cleanup_failed = {
            "ok": False,
            "errorCode": "cleanup_incomplete",
            "setupGenerationId": "generation-a",
            "collectorType": "domestic_location",
            "collectorInstanceId": "domestic-a",
        }

        with patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.status",
            return_value=current,
        ):
            self.page._collector_action_failed(
                "generation-a", "domestic_location", 3, cleanup_failed
            )

        self.assertIn("cleanup_incomplete", self.page.domestic_collector_status.text())
        self.assertFalse(self.page.retry_collector_button.isHidden())
        self.assertEqual(self.page.retry_collector_button.text(), "重试国内地点")

    def test_setup_generation_login_required_routes_to_account_management(self) -> None:
        """真实 manager 固定登录错误必须进入既有账号管理流程。"""

        self.page.runner = self._InlineRunner()
        with patch(
            "ui.douyin_commerce_page.douyin_commerce_collectors.commerce_collector_manager.begin_generation",
            side_effect=douyin_commerce_collectors.DouyinCommerceCollectorError(
                "login_required"
            ),
        ), patch.object(self.page, "_handle_login_required") as handle_login:
            self.page._start_setup_generation({"accountId": 31})

        handle_login.assert_called_once_with()
        self.assertNotIn(
            "collector_start_failed", self.page.platform_review_status.text()
        )

    class _BatchTaskStore:
        """批量执行器所需的最小内存任务存储，不触碰本机任务库。"""

        def __init__(self) -> None:
            self._next_task_id = 1
            self._tasks: dict[int, dict] = {}
            self.progress: list[dict] = []

        def create(self, batch: dict, mode: str = "oneclick_preflight") -> dict:
            del mode
            task_id = self._next_task_id
            self._next_task_id += 1
            task = {
                "id": task_id,
                "items": [
                    {"id": task_id * 100 + index}
                    for index, _item in enumerate(batch["items"], start=1)
                ],
            }
            self._tasks[task_id] = task
            return {"id": task_id}

        def get_task(self, task_id: int) -> dict | None:
            return self._tasks.get(int(task_id))

        def mark_batch_item_result(self, task_id: int, item_id: int, **kwargs) -> None:
            self.progress.append(
                {"taskId": int(task_id), "itemId": int(item_id), **kwargs}
            )

    def test_batch_page_allows_multiple_videos_and_shows_account_identity(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            videos = []
            for number, name in enumerate(("a.mp4", "b.mp4", "c.mp4"), start=1):
                path = Path(root) / name
                path.write_bytes(b"offline-video")
                videos.append({"id": number, "typeText": "视频", "storedPath": str(path), "filename": name})
            account = {"id": 71, "type": 3, "status": 1, "filePath": "douyin-71.json", "profileName": "主体", "userName": "账号"}
            with patch("ui.douyin_commerce_page.account_service.list_accounts", return_value=[account]), patch(
                "ui.douyin_commerce_page.media_service.list_media", return_value=videos
            ):
                self.page.refresh()
            self.page.account_combo.setCurrentIndex(1)
            self.page.select_video_indexes([1, 2, 3])
            self.page._sync_content_cards()

            self.assertEqual(self.page.selected_video_count(), 3)
            self.assertFalse(self.page.account_avatar.pixmap().isNull())
            self.assertIn("主体", self.page.account_card.text())
            self.assertEqual(self.page.batch_video_list.count(), 3)

    def test_resume_cancel_does_not_create_or_start_batch(self) -> None:
        """若取消确认仍创建任务或启动执行器，该测试必须失败。"""

        plan = {
            "resumeAllowed": True,
            "pendingCount": 2,
            "sourceTaskNo": "T0808-0041",
            "itemIndexes": [2, 3],
            "batch": {"items": [{"mediaPath": "/tmp/a.mp4", "scheduleTime": "2026-08-09 16:30"}, {"mediaPath": "/tmp/b.mp4", "scheduleTime": "2026-08-09 17:00"}]},
        }
        with patch("ui.douyin_commerce_page.task_service.prepare_douyin_batch_resume", return_value=plan), patch(
            "ui.douyin_commerce_page.DouyinCommerceBatchResumeConfirmDialog.exec", return_value=QDialog.DialogCode.Rejected
        ), patch("ui.douyin_commerce_page.task_service.create_douyin_batch_resume") as create_resume, patch.object(
            self.page, "start_batch_publish"
        ) as start:
            self.page.open_batch_resume(41)
        create_resume.assert_not_called()
        start.assert_not_called()

    def test_resume_confirmation_creates_child_then_starts_existing_batch_executor_path(self) -> None:
        """若确认后未创建子任务或跳过既有启动路径，该测试必须失败。"""

        plan = {
            "resumeAllowed": True,
            "pendingCount": 1,
            "sourceTaskNo": "T0808-0041",
            "itemIndexes": [2],
            "batch": {"items": [{"mediaPath": "/tmp/a.mp4", "scheduleTime": "2026-08-09 16:30"}]},
        }
        created = {"task": {"id": 99, "mode": "oneclick_resume"}, "batch": plan["batch"]}
        with patch("ui.douyin_commerce_page.task_service.prepare_douyin_batch_resume", return_value=plan), patch(
            "ui.douyin_commerce_page.DouyinCommerceBatchResumeConfirmDialog.exec", return_value=QDialog.DialogCode.Accepted
        ), patch("ui.douyin_commerce_page.task_service.create_douyin_batch_resume", return_value=created) as create_resume, patch.object(
            self.page, "start_batch_publish"
        ) as start:
            self.page.open_batch_resume(41)
        create_resume.assert_called_once()
        start.assert_called_once_with(plan["batch"], created["task"])

    def test_batch_location_rerender_replaces_old_scroll_body_without_overlapping_cards(self) -> None:
        """地点搜索回读后重绘只能保留一套卡片，不能累积旧的嵌套列布局。"""

        for index in range(3):
            self.page.video_combo.addItem(
                f"视频 {index + 1}.mp4",
                {
                    "id": index + 1,
                    "storedPath": f"/tmp/batch-{index + 1}.mp4",
                    "filename": f"视频 {index + 1}.mp4",
                },
            )
        self.page._selected_video_indexes = [1, 2, 3]
        self.page._render_batch_item_rows()
        first_body = self.page.batch_item_rows.widget()
        self.page._render_batch_item_rows()
        second_body = self.page.batch_item_rows.widget()

        self.assertIsNot(first_body, second_body)
        self.assertEqual(
            len(second_body.findChildren(QFrame, "douyinCommerceBatchItemRow")),
            3,
        )
        self.assertIsNotNone(second_body.layout().itemAtPosition(0, 0))
        self.assertIsNotNone(second_body.layout().itemAtPosition(1, 0))
        self.assertIsNone(second_body.layout().itemAtPosition(0, 1))

    def test_batch_location_rows_keep_three_to_seven_width_and_fixed_control_heights(self) -> None:
        """地点行须保持视频 3、地点 7 的宽度比例，避免下拉框被裁切。"""

        apply_style(self.app)
        self.page.video_combo.clear()
        self.page.video_combo.addItem("请选择视频", None)
        self.page.video_combo.addItem(
            "超长视频标题用于测试布局.mp4",
            {
                "storedPath": "/tmp/location-ratio.mp4",
                "filename": "超长视频标题用于测试布局.mp4",
            },
        )
        self.page._selected_video_indexes = [1]
        self.page._render_batch_item_rows()
        row = self.page.batch_item_rows.widget().findChild(
            QFrame, "douyinCommerceBatchItemRow"
        )
        combo = row.findChild(QComboBox, "douyinCommerceBatchLocationCandidates")

        self.assertEqual(row.minimumHeight(), 35)
        self.assertEqual(row.maximumHeight(), 35)
        self.assertEqual(row.layout().stretch(0), 3)
        self.assertEqual(row.layout().stretch(1), 7)
        self.assertEqual(combo.minimumHeight(), 30)
        self.assertEqual(combo.maximumHeight(), 30)
        self.assertEqual(self.page.batch_item_rows.widget().layout().verticalSpacing(), 0)
        self.page.show()
        self.app.processEvents()
        self.assertEqual(combo.height(), 30)

    def test_batch_review_rows_wrap_text_without_horizontal_scrollbar(self) -> None:
        """逐条发布信息应自动换行，不能出现横向滚动条。"""

        self.page.video_combo.clear()
        self.page.video_combo.addItem("请选择视频", None)
        self.page.video_combo.addItem(
            "非常长的视频标题用于检查自动换行.mp4",
            {
                "storedPath": "/tmp/review-wrap.mp4",
                "filename": "非常长的视频标题用于检查自动换行.mp4",
            },
        )
        self.page._selected_video_indexes = [1]
        self.page._batch_locations["/tmp/review-wrap.mp4"] = {
            "name": "非常长的地点名称",
            "address": "这是用于验证检查提交区域自动换行行为的一段很长很长的完整地址信息",
        }
        self.page._render_batch_review_rows()
        labels = self.page.batch_review_rows.widget().findChildren(QLabel)

        self.assertEqual(
            self.page.batch_review_rows.horizontalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )
        self.assertTrue(all(label.wordWrap() for label in labels))

    def test_batch_review_uses_original_filename_and_copies_selectable_information(self) -> None:
        """逐条发布信息只能展示原始文件名，且可选择、可一键复制。"""

        path = "/tmp/2e5f2bc0-9216-11f1-b29f-1831bfcc9866_北海探店.mp4"
        self.page.video_combo.clear()
        self.page.video_combo.addItem("请选择视频", None)
        self.page.video_combo.addItem(
            "2e5f2bc0-9216-11f1-b29f-1831bfcc9866_北海探店.mp4",
            {"storedPath": path, "filename": Path(path).name},
        )
        self.page._selected_video_indexes = [1]
        self.page._batch_locations[path] = {
            "name": "北海银滩",
            "address": "广西壮族自治区北海市银海区银滩大道",
        }
        self.page._render_batch_review_rows()
        labels = self.page.batch_review_rows.widget().findChildren(QLabel)

        self.assertTrue(any("视频名称：北海探店.mp4" == label.text() for label in labels))
        self.assertFalse(any("2e5f2bc0-9216-11f1-b29f-1831bfcc9866" in label.text() for label in labels))
        self.assertTrue(
            all(
                label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse
                for label in labels
            )
        )
        with patch("ui.douyin_commerce_page.QApplication.clipboard") as clipboard:
            self.page.copy_batch_publish_information()

        copied = clipboard.return_value.setText.call_args.args[0]
        self.assertIn("视频名称：北海探店.mp4", copied)
        self.assertIn("地点：北海银滩", copied)
        self.assertIn("发布时间：直接发布", copied)

    def test_batch_location_search_auto_fills_only_unassigned_videos_in_order(self) -> None:
        """地点搜索成功后，应跳过已有地点并按候选顺序填充其余视频。"""

        self.page.video_combo.clear()
        self.page.video_combo.addItem("请选择视频", None)
        paths = []
        for index in range(3):
            path = f"/tmp/auto-location-{index + 1}.mp4"
            paths.append(path)
            self.page.video_combo.addItem(
                f"视频 {index + 1}.mp4",
                {"id": index + 1, "storedPath": path, "filename": f"视频 {index + 1}.mp4"},
            )
        self.page._selected_video_indexes = [1, 2, 3]
        self.page._batch_locations[paths[1]] = {
            "poiId": "existing", "name": "已选择地点", "address": "原有地址", "scope": "domestic",
        }
        candidates = [
            {"poiId": "poi-1", "name": "候选一", "address": "地址一"},
            {"poiId": "poi-2", "name": "候选二", "address": "地址二"},
            {"poiId": "poi-3", "name": "候选三", "address": "地址三"},
        ]

        with patch(
            "ui.douyin_commerce_page.save_location_preset",
            side_effect=lambda _account_id, candidate, scope: {**candidate, "scope": scope},
        ) as save_preset:
            self.page._batch_location_search_succeeded("domestic", "测试", candidates)

        self.assertEqual(self.page._batch_locations[paths[0]]["poiId"], "poi-1")
        self.assertEqual(self.page._batch_locations[paths[1]]["poiId"], "existing")
        self.assertEqual(self.page._batch_locations[paths[2]]["poiId"], "poi-2")
        self.assertEqual(save_preset.call_count, 2)
        self.assertIn("自动填充 2 条", self.page.batch_item_settings_status.text())
        # 测试页未 show()，isVisible() 会受父窗口影响；isHidden() 才能验证
        # 组件是否被本次搜索回读显式展示。
        self.assertFalse(self.page.batch_item_settings_status.isHidden())

    def test_batch_defaults_to_immediate_and_only_generates_interval_when_enabled(self) -> None:
        self.page.video_combo.clear()
        self.page.video_combo.addItem("请选择视频", None)
        for index in range(3):
            self.page.video_combo.addItem(f"{index}.mp4", {"storedPath": f"/tmp/{index}.mp4", "filename": f"{index}.mp4"})
        self.page._refresh_batch_video_list()
        self.page.select_video_indexes([1, 2, 3])

        self.assertFalse(self.page.batch_timer_enabled.isChecked())
        self.assertEqual(self.page.item_schedule_text(1), "立即发布")
        self.page.batch_timer_enabled.setChecked(True)
        self.page.batch_interval_minutes.setValue(30)
        selected = datetime.fromisoformat(
            f"{self.page.schedule_date.date().toString('yyyy-MM-dd')} "
            f"{self.page.schedule_time.time().toString('HH:mm')}"
        )
        self.assertEqual(
            self.page.item_schedule_text(1),
            (selected + timedelta(minutes=30)).strftime("%H:%M"),
        )
        self.page.set_item_schedule_override(2, "2026-08-07 15:00")
        self.assertEqual(self.page.item_schedule_text(2), "15:00")

    def test_batch_declaration_change_stays_local_until_final_submission(self) -> None:
        """批量修改声明不可重复访问编辑页，最终提交才逐条写入并回读。"""

        self.page.video_combo.clear()
        self.page.video_combo.addItem("请选择视频", None)
        self.page.video_combo.addItem(
            "视频 1.mp4",
            {"storedPath": "/tmp/declaration-local.mp4", "filename": "视频 1.mp4"},
        )
        self.page._selected_video_indexes = [1]
        self.page._session_id = "session-demo"
        self.page._sync_view()

        with patch.object(self.page, "_start_declaration_write") as write:
            self.page.declaration_buttons["内容由AI生成"].click()

        write.assert_not_called()
        self.assertEqual(self.page._selected_declaration(), "内容由AI生成")
        self.assertEqual(self.page._confirmed_declaration, "内容由AI生成")
        self.assertFalse(self.page.declaration_status.isVisible())

    def test_batch_schedule_date_time_and_interval_share_one_row(self) -> None:
        """批量定时的日期、时间与间隔应并列显示，避免间隔被拆到下一行。"""

        schedule_row = self.page.batch_schedule_row

        self.assertGreaterEqual(schedule_row.indexOf(self.page.schedule_date), 0)
        self.assertGreaterEqual(schedule_row.indexOf(self.page.schedule_time), 0)
        self.assertGreaterEqual(schedule_row.indexOf(self.page.batch_schedule_controls), 0)

    def test_auto_schedule_date_rolls_forward_when_default_has_expired(self) -> None:
        """默认日期跨日后应刷新为下一个北京时间，而非保留过期日期。"""

        self.page._schedule_date_auto_default = True
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        yesterday = QDate(today.year, today.month, today.day).addDays(-1)
        blocked = self.page.schedule_date.blockSignals(True)
        self.page.schedule_date.setMinimumDate(yesterday)
        self.page.schedule_date.setDate(yesterday)
        self.page.schedule_date.blockSignals(blocked)

        self.page._refresh_schedule_default()

        self.assertEqual(
            self.page.schedule_date.date().toString("yyyy-MM-dd"),
            self.page._default_schedule_datetime().date().isoformat(),
        )

    def test_default_schedule_is_always_next_beijing_day_at_1600(self) -> None:
        """无论当前几点，自动默认日期都必须是北京时间次日。"""

        before = datetime(2026, 8, 8, 15, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
        after = datetime(2026, 8, 8, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

        self.assertEqual(
            self.page._default_schedule_datetime(before).strftime("%Y-%m-%d %H:%M"),
            "2026-08-09 16:00",
        )
        self.assertEqual(
            self.page._default_schedule_datetime(after).strftime("%Y-%m-%d %H:%M"),
            "2026-08-09 16:00",
        )

    def test_restoring_today_schedule_uses_next_beijing_day_default(self) -> None:
        """恢复草稿不得用今天的旧排期覆盖“次日 16:00”默认值。"""

        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        saved = {
            "updatedAt": "2026-08-08 14:46",
            "payload": {
                "schemaVersion": 3,
                "accountId": 72,
                "accountFile": "douyin-72.json",
                "shared": {
                    "title": "标题",
                    "description": "文案",
                    "tags": ["测试"],
                    "selectedMusic": {},
                    "contentDeclaration": "无需添加自主声明",
                },
                "lastLocationSearch": {"scope": "domestic", "keyword": ""},
                "publishMode": "interval-schedule",
                "schedule": {
                    "timezone": "Asia/Shanghai",
                    "startTime": f"{today.isoformat()} 09:00",
                    "intervalMinutes": 30,
                },
                "items": [
                    {
                        "mediaPath": "/tmp/saved-schedule.mp4",
                        "locationPresetId": "",
                        "locationPreset": {},
                        "enableTimer": True,
                        "scheduleTimeOverride": "",
                    }
                ],
            },
        }

        with patch(
            "ui.douyin_commerce_page.douyin_commerce_batch_draft_service.load_batch_draft",
            return_value=saved,
        ), patch.object(self.page, "_current_location_presets", return_value=[]):
            self.page.restore_batch_content()

        expected_date = today + timedelta(days=1)
        self.assertEqual(
            self.page.batch_start_date.date().toString("yyyy-MM-dd"),
            expected_date.isoformat(),
        )
        self.assertEqual(
            self.page.batch_start_time.time().toString("HH:mm"),
            "16:00",
        )

    def test_pause_button_requires_explicit_confirmation_before_requesting_pause(self) -> None:
        """验证成功默认继续；单次误触暂停按钮不得停止后续视频。"""

        layout = self.page.review_action_dock.layout()
        self.assertIs(layout.itemAt(4).widget(), self.page.pause_batch_button)
        self.page._batch_task_id = 9
        with patch.object(
            self.page.runner,
            "is_running",
            return_value=True,
        ), patch(
            "ui.douyin_commerce_page.QMessageBox.question",
            return_value=QMessageBox.StandardButton.No,
        ), patch.object(
            self.page._batch_executor,
            "request_pause",
            return_value=True,
        ) as pause:
            self.page._sync_view()
            self.assertFalse(self.page.pause_batch_button.isHidden())
            self.page.pause_batch_publish()

        pause.assert_not_called()
        self.assertFalse(self.page._batch_pause_requested)

    def test_confirmed_pause_records_client_source_and_requests_safe_pause(self) -> None:
        """只有用户在客户端明确确认，才允许当前视频后暂停。"""

        self.page._batch_task_id = 9
        with patch.object(
            self.page.runner,
            "is_running",
            return_value=True,
        ), patch(
            "ui.douyin_commerce_page.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ) as question, patch.object(
            self.page._batch_executor,
            "request_pause",
            return_value=True,
        ) as pause, patch(
            "ui.douyin_commerce_page.task_service.record_task_event"
        ) as record_event:
            self.page._sync_view()
            self.page.pause_batch_publish()

        question.assert_called_once()
        pause.assert_called_once_with(source="user_confirmed")
        record_event.assert_called_once_with(
            9,
            "batch_pause_requested",
            "用户已在客户端确认：当前视频完成后暂停后续发布",
            level="warning",
        )
        self.assertTrue(self.page._batch_pause_requested)
        self.assertIn("当前视频", self.page.validation_label.text())

    def test_collect_batch_payload_generates_explicit_item_timer_fields_before_ui_task_creation(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            video = Path(root) / "collect.mp4"
            video.write_bytes(b"offline-video")
            account = {"id": 78, "type": 3, "status": 1, "filePath": "douyin-78.json", "profileName": "主体", "userName": "账号"}
            media = {"id": 1, "typeText": "视频", "storedPath": str(video), "filename": video.name}
            with patch("ui.douyin_commerce_page.account_service.list_accounts", return_value=[account]), patch(
                "ui.douyin_commerce_page.media_service.list_media", return_value=[media]
            ):
                self.page.refresh()
            self.page.account_combo.setCurrentIndex(1)
            self.page.select_video_indexes([1])
            self.page.description_input.setPlainText("用于验证客户端任务前的逐条排期字段。")
            self.page._selected_music = {
                "musicId": "music-001", "title": "测试音乐", "creator": "测试", "duration": "01:08"
            }
            self.page._set_selected_declaration("无需添加自主声明")
            self.page._batch_locations[str(video)] = {
                "poiId": "poi-001", "name": "北海银滩景区",
                "address": "广西壮族自治区北海市银海区银滩大道中段", "scope": "domestic",
            }

            payload = self.page.collect_batch_payload()

        self.assertIs(payload["items"][0]["enableTimer"], False)
        self.assertNotIn("scheduleTime", payload["items"][0])

    def test_one_selected_video_uses_batch_preflight_instead_of_legacy_single_flow(self) -> None:
        """1 条也必须走 1..20 批量入口，不能悄悄退回旧单视频发布。"""

        with tempfile.TemporaryDirectory() as root:
            video = Path(root) / "one.mp4"
            video.write_bytes(b"offline-video")
            account = {
                "id": 72,
                "type": 3,
                "status": 1,
                "filePath": "douyin-72.json",
                "profileName": "主体",
                "userName": "账号",
            }
            media = {
                "id": 1,
                "typeText": "视频",
                "storedPath": str(video),
                "filename": "one.mp4",
            }
            with patch("ui.douyin_commerce_page.account_service.list_accounts", return_value=[account]), patch(
                "ui.douyin_commerce_page.media_service.list_media", return_value=[media]
            ):
                self.page.refresh()
            self.page.account_combo.setCurrentIndex(1)
            self.page.select_video_indexes([1])
            with patch.object(self.page, "start_batch_preflight") as batch, patch.object(
                self.page, "collect_payload"
            ) as legacy:
                self.page.start_preflight()

            batch.assert_called_once()
        legacy.assert_not_called()

    def test_batch_review_directly_enables_submit_without_preflight(self) -> None:
        """批量第三阶段不应要求先上传一轮预检，确认提交只执行一次正式上传。"""

        with tempfile.TemporaryDirectory() as root:
            video_path = Path(root) / "direct-submit.mp4"
            video_path.write_bytes(b"offline-video")
            self.page.account_combo.addItem(
                "账号 · 主体",
                {
                    "id": 1,
                    "type": 3,
                    "status": 1,
                    "filePath": "douyin-1.json",
                    "userName": "账号",
                    "profileName": "主体",
                },
            )
            self.page.account_combo.setCurrentIndex(1)
            self.page.video_combo.addItem(
                video_path.name,
                {"id": 1, "storedPath": str(video_path), "filename": video_path.name},
            )
            video_index = self.page.video_combo.count() - 1
            self.page._selected_video_indexes = [video_index]
            self.page.description_input.setPlainText("批量直接提交测试文案")
            self.page._selected_music = {
                "musicId": "music-1",
                "title": "收藏歌",
                "creator": "作者",
                "duration": "00:30",
            }
            self.page._set_selected_declaration("无需添加自主声明")
            video = self.page._selected_videos()[0]
            self.page._batch_locations[str(video.get("storedPath") or "")] = {
                "poiId": "poi-1",
                "name": "北海银滩景区",
                "address": "广西壮族自治区北海市银海区银滩大道中段",
                "scope": "domestic",
            }
            self.page.pages.setCurrentIndex(2)
            self.page._sync_view()

            self.assertTrue(self.page.preflight_button.isHidden())
            self.assertTrue(self.page.submit_button.isEnabled())
            self.assertIn("确认提交 1 条视频", self.page.submit_button.text())

    def test_batch_draft_roundtrip_preserves_mode_schedule_and_item_overrides(self) -> None:
        """本地草稿恢复不能丢失立即/间隔模式、北京时间起点或逐条覆盖。"""

        normalized = douyin_commerce_batch_draft_service.normalize_batch_draft(
            {
                "accountId": 72,
                "accountFile": "douyin-72.json",
                "shared": {"title": "标题", "description": "文案", "tags": ["北海"]},
                "publishMode": "interval-schedule",
                "schedule": {
                    "timezone": "Asia/Shanghai",
                    "startTime": "2026-08-10 09:00",
                    "intervalMinutes": 45,
                },
                "items": [
                    {
                        "mediaPath": "/tmp/a.mp4",
                        "locationPresetId": "poi-a",
                        "scheduleTimeOverride": "",
                    },
                    {
                        "mediaPath": "/tmp/b.mp4",
                        "locationPresetId": "poi-b",
                        "scheduleTimeOverride": "2026-08-10 12:00",
                    },
                ],
            }
        )

        self.assertEqual(normalized["publishMode"], "interval-schedule")
        self.assertEqual(normalized["schedule"]["timezone"], "Asia/Shanghai")
        self.assertEqual(normalized["schedule"]["startTime"], "2026-08-10 09:00")
        self.assertEqual(normalized["schedule"]["intervalMinutes"], 45)
        self.assertEqual(normalized["items"][1]["scheduleTimeOverride"], "2026-08-10 12:00")

    def test_batch_draft_payload_includes_stable_platform_intent(self) -> None:
        """保存本地内容必须包含音乐、声明、地点快照和最后搜索意图。"""

        video_file = tempfile.NamedTemporaryFile(suffix=".mp4")
        self.addCleanup(video_file.close)
        video_path = video_file.name
        account = {
            "id": 72,
            "type": 3,
            "status": 1,
            "filePath": "douyin-72.json",
            "profileName": "主体",
            "userName": "账号",
        }
        media = {
            "id": 1,
            "typeText": "视频",
            "storedPath": video_path,
            "filename": "a.mp4",
        }
        with patch(
            "ui.douyin_commerce_page.account_service.list_accounts",
            return_value=[account],
        ), patch(
            "ui.douyin_commerce_page.media_service.list_media",
            return_value=[media],
        ):
            self.page.refresh()
        self.page.account_combo.setCurrentIndex(1)
        self.page.select_video_indexes([1])
        self.page._selected_music = {
            "musicId": "music-1",
            "title": "收藏歌",
            "creator": "作者",
            "duration": "00:30",
        }
        self.page._set_selected_declaration("无需添加自主声明")
        self.page._batch_locations[video_path] = {
            "id": "preset-1",
            "poiId": "poi-1",
            "name": "夜南香北京烤鸭",
            "address": "陕西省安康市汉滨区江北办富民街2号",
            "scope": "domestic",
            "verifiedAt": "2026-08-08 15:00",
        }
        self.page._batch_location_searches["__shared_location_search__"] = {
            "scope": "domestic",
            "keyword": "夜南香北京烤鸭",
            "candidates": [{"poiId": "temporary"}],
        }

        payload = self.page._batch_draft_payload()

        self.assertEqual(payload["shared"]["selectedMusic"]["musicId"], "music-1")
        self.assertEqual(
            payload["shared"]["contentDeclaration"],
            "无需添加自主声明",
        )
        self.assertEqual(
            payload["lastLocationSearch"],
            {"scope": "domestic", "keyword": "夜南香北京烤鸭"},
        )
        self.assertEqual(
            payload["items"][0]["locationPreset"]["address"],
            "陕西省安康市汉滨区江北办富民街2号",
        )

    def test_restore_batch_content_uses_snapshot_and_drops_saved_candidates(self) -> None:
        """预设表缺失时仍恢复完整地点，但候选必须等待新会话重新读取。"""

        video_file = tempfile.NamedTemporaryFile(suffix=".mp4")
        self.addCleanup(video_file.close)
        video_path = video_file.name
        account = {
            "id": 72,
            "type": 3,
            "status": 1,
            "filePath": "douyin-72.json",
            "profileName": "主体",
            "userName": "账号",
        }
        media = {
            "id": 1,
            "typeText": "视频",
            "storedPath": video_path,
            "filename": "a.mp4",
        }
        snapshot = {
            "id": "preset-1",
            "poiId": "poi-1",
            "name": "夜心数码",
            "address": "广西壮族自治区北海市海城区测试路1号",
            "scope": "local",
            "verifiedAt": "2026-08-08 15:00",
        }
        saved = {
            "updatedAt": "2026-08-08 17:00",
            "payload": {
                "schemaVersion": 3,
                "accountId": 72,
                "accountFile": "douyin-72.json",
                "shared": {
                    "title": "标题",
                    "description": "文案",
                    "tags": ["测试"],
                    "selectedMusic": {
                        "musicId": "music-1",
                        "title": "收藏歌",
                        "creator": "作者",
                        "duration": "00:30",
                        "source": "douyin-favorite-visible",
                    },
                    "contentDeclaration": "无需添加自主声明",
                },
                "lastLocationSearch": {
                    "scope": "local",
                    "keyword": "夜心数码",
                    "candidates": [{"poiId": "must-not-restore"}],
                },
                "publishMode": "immediate",
                "schedule": {
                    "timezone": "Asia/Shanghai",
                    "startTime": "",
                    "intervalMinutes": 0,
                },
                "items": [
                    {
                        "mediaPath": video_path,
                        "locationPresetId": "preset-1",
                        "locationPreset": snapshot,
                        "enableTimer": False,
                        "scheduleTimeOverride": "",
                    }
                ],
            },
        }
        with patch(
            "ui.douyin_commerce_page.account_service.list_accounts",
            return_value=[account],
        ), patch(
            "ui.douyin_commerce_page.media_service.list_media",
            return_value=[media],
        ):
            self.page.refresh()
        with patch(
            "ui.douyin_commerce_page.douyin_commerce_batch_draft_service.load_batch_draft",
            return_value=saved,
        ), patch.object(self.page, "_current_location_presets", return_value=[]):
            self.page.restore_batch_content()

        self.assertEqual(self.page._selected_music["musicId"], "music-1")
        self.assertEqual(self.page._selected_declaration(), "无需添加自主声明")
        self.assertEqual(
            self.page._batch_locations[video_path]["address"],
            snapshot["address"],
        )
        state = self.page._batch_location_state()
        self.assertEqual(state["scope"], "local")
        self.assertEqual(state["keyword"], "夜心数码")
        self.assertEqual(state["candidates"], [])
        self.assertEqual(self.page.batch_location_scope_combo.currentData(), "local")
        self.assertEqual(self.page.batch_location_keyword.text(), "夜心数码")

    def test_batch_music_reads_current_account_cache_without_opening_session(self) -> None:
        account = {"id": 73, "type": 3, "status": 1, "filePath": "douyin-73.json", "profileName": "主体", "userName": "账号"}
        self.page.account_combo.clear()
        self.page.account_combo.addItem("请选择", None)
        self.page.account_combo.addItem("账号", account)
        self.page.account_combo.setCurrentIndex(1)
        self.page._selected_video_indexes = [1]
        self.page._session_id = ""
        rows = [{"musicId": "m-1", "title": "收藏歌", "creator": "作者", "duration": "00:30", "syncedAt": "2026-08-06 10:00"}]

        with patch(
            "ui.douyin_commerce_page.douyin_favorite_music_cache.list_cached_favorite_music",
            return_value=rows,
        ) as cached, patch(
            "ui.douyin_commerce_page.douyin_commerce_session.commerce_session_manager.start_upload"
        ) as upload:
            self.page._load_favorite_music_candidates()

        cached.assert_called_once_with(73)
        upload.assert_not_called()
        self.assertEqual(self.page.music_combo.count(), 2)
        self.assertEqual(self.page.music_combo.itemData(1)["musicId"], "m-1")

    def test_batch_content_preparation_starts_one_isolated_setup_generation(self) -> None:
        """批量平台设置只建立采集代际，不复用正式发布编辑会话。"""

        self.page._selected_video_indexes = [1, 2]
        self.page._session_id = ""
        setup_payload = {
            "accountList": ["douyin-setup.json"],
            "fileList": ["/tmp/first.mp4"],
            "title": "批量共享标题",
            "description": "批量共享文案",
            "tags": [],
        }
        with patch.object(self.page, "_batch_content_is_valid", return_value=True), patch.object(
            self.page, "_batch_content_change_kind", return_value="reupload"
        ), patch.object(
            self.page, "collect_upload_payload", return_value=setup_payload
        ), patch.object(self.page, "_start_setup_generation") as start_generation, patch.object(
            self.page, "start_upload"
        ) as start_upload:
            self.page.continue_after_content()

        start_generation.assert_called_once_with(setup_payload)
        start_upload.assert_not_called()

    def test_batch_default_declaration_and_schedule_share_the_left_platform_column(self) -> None:
        """批量默认无需声明；发布方式与间隔位于声明下方的同一共享区。"""

        self.page._selected_video_indexes = [1, 2]
        self.assertEqual(self.page._selected_declaration(), "无需添加自主声明")
        self.assertFalse(self.page.schedule_stage.isHidden())
        self.page.timer_enabled.setChecked(True)
        self.assertEqual(self.page.batch_publish_mode.currentData(), "interval-schedule")
        self.assertFalse(self.page.batch_schedule_controls.isHidden())

    def test_batch_location_candidates_are_shared_then_bound_per_video(self) -> None:
        """顶部统一搜索的候选可分别绑定到每条视频。"""

        path = "/tmp/first.mp4"
        candidate = {
            "poiId": "poi-1",
            "name": "北海银滩景区",
            "address": "广西壮族自治区北海市银海区银滩大道中段",
            "distance": "6km",
        }
        self.page._batch_location_search_succeeded("domestic", "北海", [candidate])
        self.assertEqual(
            self.page._batch_location_searches["__shared_location_search__"]["candidates"][0]["poiId"],
            "poi-1",
        )
        account = {"id": 99, "type": 3, "status": 1, "filePath": "douyin-99.json"}
        self.page.account_combo.clear()
        self.page.account_combo.addItem("账号", account)
        with patch(
            "ui.douyin_commerce_page.save_location_preset",
            return_value={**candidate, "scope": "domestic", "id": "preset-1"},
        ) as save:
            self.page._select_batch_location_candidate(path, "domestic", candidate)

        save.assert_called_once_with(99, candidate, "domestic")
        self.assertEqual(self.page._batch_locations[path]["address"], candidate["address"])

    def test_batch_location_search_results_are_available_to_every_video_dropdown(self) -> None:
        """所有视频行复用顶部搜索结果，界面不再各自创建搜索框。"""

        for index in range(2):
            self.page.video_combo.addItem(
                f"视频 {index + 1}.mp4",
                {
                    "id": index + 1,
                    "storedPath": f"/tmp/shared-location-{index + 1}.mp4",
                    "filename": f"视频 {index + 1}.mp4",
                },
            )
        self.page._selected_video_indexes = [1, 2]
        candidate = {
            "poiId": "poi-shared",
            "name": "北海银滩景区",
            "address": "广西壮族自治区北海市银海区银滩大道中段",
        }

        self.page._batch_location_search_succeeded("domestic", "北海", [candidate])
        body = self.page.batch_item_rows.widget()
        dropdowns = body.findChildren(QComboBox, "douyinCommerceBatchLocationCandidates")

        self.assertEqual(len(dropdowns), 2)
        self.assertTrue(all(dropdown.count() == 2 for dropdown in dropdowns))
        self.assertTrue(all(dropdown.isEnabled() for dropdown in dropdowns))
        self.assertTrue(
            all(dropdown.itemData(1)["poiId"] == "poi-shared" for dropdown in dropdowns)
        )
        self.assertEqual(
            len(body.findChildren(QLineEdit, "douyinCommerceBatchLocationKeyword")),
            0,
        )

    def test_batch_location_search_error_remains_visible_after_rerender(self) -> None:
        """平台搜索失败时必须保留诊断，不能被通用提示覆盖。"""

        self.page._batch_location_search_failed("当前编辑页未返回与关键词相符的地点")

        self.assertIn("地点候选读取失败", self.page.batch_item_settings_status.text())
        self.assertIn("关键词", self.page.batch_item_settings_status.text())
        self.assertFalse(self.page.batch_item_settings_status.isHidden())

    def test_batch_location_dropdown_survives_unrelated_platform_state_sync(self) -> None:
        """状态刷新不能替换用户正准备点击的地点下拉框。"""

        self.page.video_combo.addItem(
            "视频 1.mp4",
            {
                "id": 1,
                "storedPath": "/tmp/location-stable.mp4",
                "filename": "视频 1.mp4",
            },
        )
        self.page._selected_video_indexes = [1]
        candidate = {
            "poiId": "poi-stable",
            "name": "北海银滩景区",
            "address": "广西壮族自治区北海市银海区银滩大道中段",
        }
        self.page._batch_location_search_succeeded("domestic", "北海", [candidate])
        body = self.page.batch_item_rows.widget()
        dropdown = body.findChild(QComboBox, "douyinCommerceBatchLocationCandidates")

        self.page._sync_platform_workspace()

        self.assertIs(self.page.batch_item_rows.widget(), body)
        self.assertIs(
            body.findChild(QComboBox, "douyinCommerceBatchLocationCandidates"),
            dropdown,
        )
        self.assertTrue(dropdown.isEnabled())

    def test_batch_location_search_auto_binds_first_unassigned_candidate(self) -> None:
        """地点搜索成功后应自动把候选绑定到首条未设置视频。"""

        path = "/tmp/location-clickable.mp4"
        self.page.video_combo.clear()
        self.page.video_combo.addItem("请选择视频", None)
        self.page.video_combo.addItem(
            "视频 1.mp4",
            {"id": 1, "storedPath": path, "filename": "视频 1.mp4"},
        )
        self.page._selected_video_indexes = [1]
        account = {"id": 97, "type": 3, "status": 1, "filePath": "douyin-97.json"}
        self.page.account_combo.clear()
        self.page.account_combo.addItem("账号", account)
        candidate = {
            "poiId": "poi-clickable",
            "name": "北海银滩景区",
            "address": "广西壮族自治区北海市银海区银滩大道中段",
        }
        with patch(
            "ui.douyin_commerce_page.save_location_preset",
            return_value={**candidate, "scope": "domestic", "id": "preset-clickable"},
        ) as save:
            self.page._batch_location_search_succeeded("domestic", "北海", [candidate])

        save.assert_called_once_with(97, candidate, "domestic")
        self.assertEqual(self.page._batch_locations[path]["poiId"], "poi-clickable")
        dropdown = self.page.batch_item_rows.widget().findChild(
            QComboBox, "douyinCommerceBatchLocationCandidates"
        )
        self.assertEqual(dropdown.currentIndex(), 1)

    def test_batch_location_placeholder_clears_old_binding_then_next_search_refills(self) -> None:
        """用户改回“请选择地点”后，旧绑定必须真正清除并允许新搜索重新自动填入。"""

        path = "/tmp/location-reselect.mp4"
        self.page.video_combo.clear()
        self.page.video_combo.addItem("请选择视频", None)
        self.page.video_combo.addItem(
            "视频 1.mp4",
            {"id": 1, "storedPath": path, "filename": "视频 1.mp4"},
        )
        self.page._selected_video_indexes = [1]
        account = {"id": 98, "type": 3, "status": 1, "filePath": "douyin-98.json"}
        self.page.account_combo.clear()
        self.page.account_combo.addItem("账号", account)
        old_candidate = {
            "poiId": "poi-old",
            "name": "旧地点",
            "address": "旧地点完整地址",
        }
        new_candidate = {
            "poiId": "poi-new",
            "name": "新地点",
            "address": "新地点完整地址",
        }

        def save_candidate(_account_id, candidate, scope):
            return {**candidate, "scope": scope, "id": f"preset-{candidate['poiId']}"}

        with patch(
            "ui.douyin_commerce_page.save_location_preset",
            side_effect=save_candidate,
        ) as save:
            self.page._batch_location_search_succeeded(
                "domestic", "旧关键词", [old_candidate]
            )
            dropdown = self.page.batch_item_rows.widget().findChild(
                QComboBox, "douyinCommerceBatchLocationCandidates"
            )
            self.assertEqual(dropdown.currentData()["poiId"], "poi-old")

            dropdown.setCurrentIndex(0)

            self.assertNotIn(path, self.page._batch_locations)
            rerendered_dropdown = self.page.batch_item_rows.widget().findChild(
                QComboBox, "douyinCommerceBatchLocationCandidates"
            )
            self.assertEqual(rerendered_dropdown.currentIndex(), 0)
            self.assertEqual(rerendered_dropdown.currentText(), "请选择地点")

            self.page._batch_location_search_succeeded(
                "domestic", "新关键词", [new_candidate]
            )

        self.assertEqual(save.call_count, 2)
        self.assertEqual(self.page._batch_locations[path]["poiId"], "poi-new")
        final_dropdown = self.page.batch_item_rows.widget().findChild(
            QComboBox, "douyinCommerceBatchLocationCandidates"
        )
        self.assertEqual(final_dropdown.currentData()["poiId"], "poi-new")

    def test_batch_location_selected_value_is_only_shown_in_dropdown(self) -> None:
        """已选地点只保留在下拉框中，避免视频卡片重复显示完整地址。"""

        path = "/tmp/location-selected.mp4"
        self.page.video_combo.clear()
        self.page.video_combo.addItem("请选择视频", None)
        self.page.video_combo.addItem(
            "视频 1.mp4",
            {"id": 1, "storedPath": path, "filename": "视频 1.mp4"},
        )
        self.page._selected_video_indexes = [1]
        selected = {
            "poiId": "poi-selected",
            "name": "北海银滩景区",
            "address": "广西壮族自治区北海市银海区银滩大道中段",
            "scope": "domestic",
        }
        self.page._batch_locations[path] = selected
        self.page._batch_location_search_succeeded("domestic", "北海", [])
        body = self.page.batch_item_rows.widget()
        dropdown = body.findChild(QComboBox, "douyinCommerceBatchLocationCandidates")

        self.assertTrue(dropdown.isEnabled())
        self.assertGreater(dropdown.currentIndex(), 0)
        self.assertIn("北海银滩景区", dropdown.currentText())
        self.assertEqual(
            body.findChildren(QLabel, "douyinCommerceBatchLocationSelected"), []
        )

    def test_batch_music_selection_with_setup_session_stays_local_until_preflight(self) -> None:
        """批量选择音乐只更新本地配置，预检时才逐条写入平台。"""

        candidate = {"musicId": "music-1", "title": "收藏歌", "creator": "作者", "duration": "00:30"}
        self.page._selected_video_indexes = [1, 2]
        self.page._session_id = "setup-session"
        self.page._show_music_candidates([candidate], source="session")
        with patch.object(self.page, "_start_music_write") as write:
            self.page._music_combo_activated(1)

        write.assert_not_called()
        self.assertEqual(self.page._selected_music, candidate)
        self.assertEqual(self.page.music_combo.currentData(), candidate)
        self.assertIn("收藏歌", self.page.music_combo.currentText())
        self.assertFalse(self.page.music_status.isVisible())

    def test_batch_platform_read_writes_favorite_music_to_local_cache(self) -> None:
        """已读取的收藏音乐须落盘，下一批无需再次刷新才能加载。"""

        account = {"id": 96, "type": 3, "status": 1, "filePath": "douyin-96.json"}
        candidate = {"musicId": "music-96", "title": "本地缓存歌", "creator": "作者", "duration": "00:30"}
        self.page.account_combo.clear()
        self.page.account_combo.addItem("账号", account)
        self.page.account_combo.setCurrentIndex(0)
        self.page._selected_video_indexes = [1]

        with patch(
            "ui.douyin_commerce_page.douyin_favorite_music_cache.replace_cached_favorite_music"
        ) as replace:
            self.page._show_music_candidates([candidate], source="session")

        replace.assert_called_once_with(96, [candidate])

    def test_batch_cached_music_candidate_click_selects_locally_without_editor_write(self) -> None:
        """未上传时点击本地收藏候选，只更新批量草稿，不访问编辑会话。"""

        candidate = {
            "musicId": "m-local-1",
            "title": "本地收藏歌",
            "creator": "作者",
            "duration": "00:30",
        }
        self.page._selected_video_indexes = [1]
        self.page._session_id = ""
        self.page._batch_preflight_fingerprint = "旧预检"
        self.page._show_music_candidates([candidate], source="account-cache")

        with patch.object(self.page, "_start_music_write") as write:
            self.page.music_candidate_list.itemClicked.emit(
                self.page.music_candidate_list.item(0)
            )
            self.app.processEvents()

        write.assert_not_called()
        self.assertEqual(self.page._selected_music, candidate)
        self.assertEqual(self.page._batch_preflight_fingerprint, "")
        self.assertFalse(self.page.music_status.isVisible())

    def test_batch_editor_session_end_explains_rebuild_for_shared_content_changes(self) -> None:
        """批量会话关闭后只改共享字段，页面必须说明下次预检会重建会话。"""

        self.page._selected_video_indexes = [1]
        self.page._session_id = ""
        self.page._uploaded_editor_payload = None
        self.page._batch_editor_session_ended = True

        self.page._sync_view()

        expected = "编辑会话已结束；预检将为每条视频重新建立上传会话"
        self.assertFalse(self.page.content_notice.isHidden())
        self.assertIn(expected, self.page.content_notice.text())
        self.assertTrue(self.page.platform_session_status.isHidden())
        self.assertIn(expected, self.page.platform_review_status.text())
        self.assertEqual(self.page._batch_content_change_kind(), "reupload")

    def test_batch_preflight_reuploads_every_video_after_real_title_edit_and_session_close(self) -> None:
        """页面完成预检后编辑标题，下一轮必须逐条重新创建上传会话。"""

        with tempfile.TemporaryDirectory() as root:
            videos = []
            for number in range(2):
                path = Path(root) / f"batch-{number}.mp4"
                path.write_bytes(b"offline-video")
                videos.append(
                    {
                        "id": number + 1,
                        "typeText": "视频",
                        "storedPath": str(path),
                        "filename": path.name,
                    }
                )
            account = {
                "id": 75,
                "type": 3,
                "status": 1,
                "filePath": "douyin-75.json",
                "profileName": "主体",
                "userName": "账号",
            }
            with patch("ui.douyin_commerce_page.account_service.list_accounts", return_value=[account]), patch(
                "ui.douyin_commerce_page.media_service.list_media", return_value=videos
            ):
                self.page.refresh()
            self.page.account_combo.setCurrentIndex(1)
            self.page.select_video_indexes([1, 2])
            self.page.title_input.setText("预检前标题")
            self.page.description_input.setPlainText("用于验证批量编辑会话重建的作品文案。")
            self.page._set_tags(["批量测试"])
            self.page._selected_music = {
                "musicId": "music-001",
                "title": "测试音乐",
                "creator": "测试作者",
                "duration": "01:08",
            }
            self.page._set_selected_declaration("无需添加自主声明")
            for video in videos:
                self.page._batch_locations[video["storedPath"]] = {
                    # FakeCommerceSessionManager 仅回传这个当前编辑页候选；两条
                    # 视频复用同一官方地点仍会分别走一次真实匹配与写入回读。
                    "poiId": "poi-001",
                    "name": "北海银滩景区",
                    "address": "广西壮族自治区北海市银海区银滩大道中段",
                    "scope": "domestic",
                }

            manager = FakeCommerceSessionManager()
            task_store = self._BatchTaskStore()
            self.page._batch_executor = DouyinCommerceBatchExecutor(
                manager,
                task_store=task_store,
            )
            self.page.runner = self._InlineRunner()
            with patch(
                "ui.douyin_commerce_page.task_service.create_douyin_batch_task",
                side_effect=task_store.create,
            ), patch("ui.douyin_commerce_page.task_service.mark_task_running"):
                self.page.start_batch_preflight()

                self.assertTrue(self.page._batch_preflight_fingerprint)
                self.assertTrue(self.page._batch_editor_session_ended)
                self.assertEqual(
                    [call for call in manager.calls if call.startswith("start_upload:")],
                    ["start_upload:0", "start_upload:1"],
                )

                # 真实控件编辑，而非直接改内部字段；Qt 信号必须清除预检指纹。
                self.page.title_input.setText("预检后的新标题")
                self.app.processEvents()
                self.assertEqual(self.page._batch_preflight_fingerprint, "")
                self.assertIn(
                    "编辑会话已结束；预检将为每条视频重新建立上传会话",
                    self.page.content_notice.text(),
                )

                self.page.start_batch_preflight()

            self.assertEqual(
                [call for call in manager.calls if call.startswith("start_upload:")],
                [
                    "start_upload:0",
                    "start_upload:1",
                    "start_upload:2",
                    "start_upload:3",
                ],
            )
            self.assertTrue(self.page._batch_preflight_fingerprint)

    def test_batch_shared_content_replaces_setup_generation_without_legacy_sync(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            video = Path(root) / "one.mp4"
            video.write_bytes(b"offline-video")
            account = {"id": 74, "type": 3, "status": 1, "filePath": "douyin-74.json", "profileName": "主体", "userName": "账号"}
            media = {"id": 1, "typeText": "视频", "storedPath": str(video), "filename": "one.mp4"}
            with patch("ui.douyin_commerce_page.account_service.list_accounts", return_value=[account]), patch(
                "ui.douyin_commerce_page.media_service.list_media", return_value=[media]
            ):
                self.page.refresh()
            self.page.account_combo.setCurrentIndex(1)
            self.page.select_video_indexes([1])
            self.page.description_input.setPlainText("原文案")
            original = self.page.collect_upload_payload()
            self.page._session_id = "live-session"
            self.page._uploaded_editor_payload = dict(original)
            self.page.title_input.setText("更新标题")

            with patch.object(self.page, "_start_content_sync") as synchronize, patch.object(
                self.page, "start_upload"
            ) as upload, patch.object(
                self.page, "_start_setup_generation"
            ) as start_generation:
                self.page.continue_after_content()

            start_generation.assert_called_once()
            synchronize.assert_not_called()
            upload.assert_not_called()
            self.page._session_id = ""
            self.assertEqual(self.page._batch_content_change_kind(), "reupload")

    def test_batch_verification_polls_batch_task_and_cleans_up_after_finish(self) -> None:
        broker = DouyinVerificationBroker()
        request_id = broker.create_sms(task_id=975, message="需要短信验证")
        dialog = MagicMock()
        self.page._batch_task_id = 975
        with patch("ui.douyin_commerce_page.verification_broker", broker), patch(
            "ui.douyin_commerce_page.DouyinVerificationDialog", return_value=dialog
        ) as dialog_type:
            self.page._start_douyin_verification_polling()
            self.page._poll_douyin_verification()
            dialog_type.assert_called_once_with(request_id, broker=broker, parent=self.page)
            self.page._batch_operation_finished()

        self.assertIsNone(broker.request_for_task(975))
        self.assertIsNone(self.page._batch_task_id)
        self.assertFalse(self.page._douyin_verification_poll_timer.isActive())


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


class DouyinCommerceBatchRoutingTests(unittest.TestCase):
    """批次信封必须经专用执行器，不能降级为单视频或通用任务。"""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.video = Path(self.tempdir.name) / "batch-routing.mp4"
        self.video.write_bytes(b"batch-routing-video")
        self.payload = {
            "type": 3,
            "workflow": "douyin-commerce-batch",
            "commerceMode": "local-group-buy",
            "contentType": "video",
            "runtimeMode": "preflight",
            "debugDryRun": True,
            "accountList": ["oneclick_3_offline.json"],
            "shared": {
                "title": "批量路由测试",
                "description": "只验证专用批量路由，不触发浏览器。",
                "tags": ["测试"],
                "selectedMusic": {
                    "musicId": "music-routing",
                    "title": "测试收藏音乐",
                    "creator": "一键发",
                    "duration": "01:00",
                },
                "contentDeclaration": "无需添加自主声明",
            },
            "publishMode": "immediate",
            "schedule": {},
            "items": [
                {
                    "mediaPath": str(self.video),
                    "locationPreset": {
                        "poiId": "poi-routing",
                        "name": "北海银滩景区",
                        "address": "广西壮族自治区北海市银海区银滩大道中段",
                        "scope": "domestic",
                    },
                }
            ],
        }

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_publish_service_routes_batch_preflight_without_final_submit(self) -> None:
        with patch.object(
            publish_service.douyin_commerce_batch_executor.batch_executor,
            "run_preflight",
            return_value=[{"status": "preflighted"}],
        ) as preflight, patch.object(
            publish_service.task_service, "mark_task_running"
        ), patch.object(
            publish_service.task_service, "record_task_event"
        ):
            publish_service._run_preflight({"id": 41, "dryRun": 1}, [self.payload])

        preflight.assert_called_once()
        self.assertEqual(preflight.call_args.kwargs["task_id"], 41)
        self.assertNotIn("confirmed", preflight.call_args.kwargs)

    def test_batch_publish_requires_explicit_confirmed_flag(self) -> None:
        payload = {
            **self.payload,
            "runtimeMode": "publish",
            "debugDryRun": False,
            "batchConfirmed": False,
        }
        with self.assertRaisesRegex(ValueError, "批量确认"):
            publish_service._run_publish({"id": 42, "dryRun": 0}, [payload])

    def test_batch_publish_routes_only_after_all_explicit_runtime_gates(self) -> None:
        payload = {
            **self.payload,
            "runtimeMode": "publish",
            "debugDryRun": False,
            "batchConfirmed": True,
        }
        with patch.object(
            publish_service.douyin_commerce_batch_executor.batch_executor,
            "run_publish",
            return_value=[{"status": "published"}],
        ) as publish, patch.object(
            publish_service.task_service, "mark_task_running"
        ), patch.object(
            publish_service.task_service, "record_task_event"
        ):
            publish_service._run_publish({"id": 43, "dryRun": 0}, [payload])

        publish.assert_called_once()
        self.assertEqual(publish.call_args.kwargs["task_id"], 43)
        self.assertTrue(publish.call_args.kwargs["confirmed"])

    def test_desktop_batch_route_prepares_all_item_timer_fields_before_task_creation(self) -> None:
        """通用桌面入口不能把仅有用户配置的信封直接交给任务服务。"""

        captured: dict[str, object] = {}

        class FakeThread:
            def __init__(self, **kwargs) -> None:
                captured["thread"] = kwargs

            def start(self) -> None:
                captured["started"] = True

        def create(batch, **kwargs):
            captured["batch"] = batch
            captured["create_kwargs"] = kwargs
            return {"id": 909}

        with patch.object(publish_service.task_service, "create_douyin_batch_task", side_effect=create), patch.object(
            publish_service.threading, "Thread", FakeThread
        ):
            task = publish_service.start_desktop_publish([self.payload])

        self.assertEqual(task["id"], 909)
        prepared = captured["batch"]
        self.assertTrue(all(item["enableTimer"] is False for item in prepared["items"]))
        self.assertTrue(captured["started"])
        self.assertEqual(
            captured["thread"]["kwargs"]["prepared_batch"]["items"][0]["enableTimer"],
            False,
        )


if __name__ == "__main__":
    unittest.main()
