# -*- coding: utf-8 -*-
"""抖音发布定位的离线回归测试。"""

import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel

from app_core import douyin_location_service, oneclick_preflight
from app_core.publish_service import _validate_payloads
from ui.publish_page import PublishPage


class DouyinLocationMatchingTests(unittest.TestCase):
    def test_douyin_preflight_title_uses_controlled_input_fill(self) -> None:
        state = {"title": "旧标题", "description": ""}

        async def fill_title(value: str, **_kwargs) -> None:
            state["title"] = value

        async def fill_description(value: str, **_kwargs) -> None:
            state["description"] = value

        title_input = MagicMock()
        title_input.wait_for = AsyncMock()
        title_input.fill = AsyncMock(side_effect=fill_title)
        title_input.input_value = AsyncMock(side_effect=lambda: state["title"])
        title_query = MagicMock()
        title_query.first = title_input

        editor = MagicMock()
        editor.wait_for = AsyncMock()
        editor.fill = AsyncMock(side_effect=fill_description)
        editor.inner_text = AsyncMock(side_effect=lambda: state["description"])
        editor_query = MagicMock()
        editor_query.first = editor

        page = MagicMock()
        page.locator.side_effect = lambda selector: (
            title_query if selector.startswith("input[") else editor_query
        )
        page.wait_for_timeout = AsyncMock()

        asyncio.run(
            oneclick_preflight._douyin_fill_title_and_description(
                page,
                "测试",
                "测试文案",
                title_placeholder="填写作品标题，为作品获得更多流量",
                label="视频",
            )
        )

        self.assertEqual(
            [call.args[0] for call in title_input.fill.await_args_list],
            ["", "测试"],
        )
        editor.fill.assert_awaited_once_with("测试文案", timeout=10_000)

    def test_candidate_name_uses_primary_line_not_address(self) -> None:
        self.assertEqual(
            oneclick_preflight._douyin_location_candidate_name(
                "北海银滩\n广西壮族自治区北海市银海区"
            ),
            "北海银滩",
        )

    def test_exact_match_never_accepts_fuzzy_candidate(self) -> None:
        candidates = [
            "北海银滩\n北海市银海区",
            "北海银滩旅游度假区\n北海市银海区",
            "北海老街\n北海市海城区",
        ]
        self.assertEqual(
            oneclick_preflight._douyin_exact_location_indexes(
                "北海银滩", candidates
            ),
            [0],
        )

    def test_duplicate_exact_names_remain_ambiguous(self) -> None:
        self.assertEqual(
            oneclick_preflight._douyin_exact_location_indexes(
                "北海银滩",
                ["北海银滩\n地址A", "北海银滩\n地址B"],
            ),
            [0, 1],
        )

    def test_poi_id_has_priority_over_duplicate_names(self) -> None:
        self.assertEqual(
            oneclick_preflight._douyin_location_match_indexes(
                {
                    "poiId": "poi-b",
                    "name": "万达广场",
                    "address": "广东南路225号",
                },
                [
                    {"poiId": "poi-a", "name": "万达广场", "address": "上海路"},
                    {
                        "poiId": "poi-b",
                        "name": "万达广场",
                        "address": "广东南路225号",
                    },
                ],
            ),
            [1],
        )

    def test_address_disambiguates_when_page_hides_poi_id(self) -> None:
        self.assertEqual(
            oneclick_preflight._douyin_location_match_indexes(
                {
                    "poiId": "poi-b",
                    "name": "万达广场",
                    "address": "广西壮族自治区北海市银海区广东南路225号",
                },
                [
                    {"poiId": "", "name": "万达广场", "address": "上海路"},
                    {
                        "poiId": "",
                        "name": "万达广场",
                        "address": "广西壮族自治区北海市银海区广东南路225号 6.0km",
                    },
                ],
            ),
            [1],
        )

    def test_non_poi_dom_value_falls_back_to_unique_name_and_address(self) -> None:
        """新版列表的 data-value 可能是行值，不能阻断真实 POI 的唯一回退。"""

        self.assertEqual(
            oneclick_preflight._douyin_location_match_indexes(
                {
                    "poiId": "6601124346666682376",
                    "name": "北海银滩景区",
                    "address": "广西壮族自治区北海市银海区银滩大道中段",
                },
                [
                    {
                        "poiId": "row-0",
                        "name": "北海银滩景区",
                        "address": "广西壮族自治区北海市银海区银滩大道西段",
                    },
                    {
                        "poiId": "row-1",
                        "name": "北海银滩景区",
                        "address": "广西壮族自治区北海市银海区银滩大道中段",
                    },
                ],
            ),
            [1],
        )

    def test_location_control_indexes_dedupe_nested_text_nodes(self) -> None:
        """同一 semi-select 内多个 span 不应被当成多个发布定位入口。"""

        self.assertEqual(
            oneclick_preflight._douyin_location_control_indexes(
                [
                    {
                        "identity": "semi-select|location|120:420:620:40",
                        "text": "输入地理位置",
                        "context": "发布定位",
                    },
                    {
                        "identity": "semi-select|location|120:420:620:40",
                        "text": "输入地理位置",
                        "context": "发布定位",
                    },
                    {
                        "identity": "semi-select|collection|120:360:620:40",
                        "text": "不选择合集",
                        "context": "合集设置",
                    },
                ]
            ),
            [0],
        )

    def test_distinct_location_controls_remain_ambiguous(self) -> None:
        """两个真实控件不能因文案相同而盲选第一个。"""

        self.assertEqual(
            oneclick_preflight._douyin_location_control_indexes(
                [
                    {
                        "identity": "semi-select|location-a|120:420:620:40",
                        "text": "输入地理位置",
                    },
                    {
                        "identity": "semi-select|location-b|120:510:620:40",
                        "text": "输入地理位置",
                    },
                ]
            ),
            [0, 1],
        )

    def test_direct_location_control_beats_shared_parent_context(self) -> None:
        """共享父级含发布定位时，不能把合集控件误判为地点入口。"""

        shared_context = "合集/播放列表 同步设置 发布定位"
        self.assertEqual(
            oneclick_preflight._douyin_location_control_indexes(
                [
                    {
                        "identity": "semi-select|collection|120:360:620:40",
                        "text": "不选择合集",
                        "context": shared_context,
                    },
                    {
                        "identity": "semi-select|location|120:420:620:40",
                        "text": "输入地理位置",
                        "context": shared_context,
                    },
                ]
            ),
            [1],
        )

    def test_location_control_below_viewport_is_scrolled_after_unique_match(self) -> None:
        """新版扩展信息区在首屏下方时，仍应识别后再滚动，而不是提前丢弃。"""

        class FakeControl:
            def __init__(self, name: str) -> None:
                self.name = name
                self.scroll_calls = 0

            async def is_visible(self) -> bool:
                return True

            async def is_enabled(self) -> bool:
                return True

            async def scroll_into_view_if_needed(self, **_kwargs) -> None:
                self.scroll_calls += 1

        class FakeLocator:
            def __init__(self, controls) -> None:
                self.controls = controls

            async def count(self) -> int:
                return len(self.controls)

            def nth(self, index: int):
                return self.controls[index]

        class FakePage:
            def __init__(self, controls) -> None:
                self.controls = controls

            def locator(self, _selector: str):
                return FakeLocator(self.controls)

        collection = FakeControl("collection")
        location = FakeControl("location")

        async def descriptor(control):
            return {
                "identity": control.name,
                "text": "输入地理位置" if control is location else "请选择合集",
            }

        with patch.object(
            oneclick_preflight,
            "_douyin_location_control_descriptor",
            side_effect=descriptor,
        ):
            controls = asyncio.run(
                oneclick_preflight._douyin_visible_location_controls(
                    FakePage([collection, location])
                )
            )

        self.assertEqual(controls, [location])
        self.assertEqual(location.scroll_calls, 1)
        self.assertEqual(collection.scroll_calls, 0)

    def test_blank_location_skips_platform_controls(self) -> None:
        page = MagicMock()
        result = asyncio.run(
            oneclick_preflight._douyin_set_location(
                page,
                {"locationKeyword": "  "},
            )
        )
        self.assertEqual(result, "")
        page.locator.assert_not_called()


class DouyinLocationUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_location_has_independent_full_width_search_and_enters_payload(self) -> None:
        page = PublishPage()
        self.assertIsNotNone(page.douyin_sync_toutiao)
        self.assertIsNone(page.douyin_location_scope)
        self.assertIsNotNone(page.douyin_location_keyword)
        self.assertIsNot(
            page.douyin_sync_toutiao.parentWidget(),
            page.douyin_location_keyword.parentWidget(),
        )
        self.assertIs(
            page.douyin_location_search_button.parentWidget(),
            page.douyin_location_keyword.parentWidget(),
        )

        page.common_title_input.setText("定位功能测试")
        page.title_input.setPlainText("只做离线载荷测试")
        page.douyin_sync_toutiao.setChecked(True)
        page._show_douyin_location_results(
            [
                {
                    "poiId": "6601124346666682376",
                    "name": "北海银滩景区",
                    "address": "广西壮族自治区北海市银海区银滩大道中段",
                    "distance": "6.0km",
                }
            ],
            source_account_id=3,
        )
        result_item = page.douyin_location_results.item(0)
        result_card = page.douyin_location_results.itemWidget(result_item)
        address_label = result_card.findChild(QLabel, "douyinLocationAddress")
        self.assertEqual(
            address_label.text(),
            "广西壮族自治区北海市银海区银滩大道中段",
        )
        self.assertTrue(address_label.wordWrap())
        self.assertGreaterEqual(result_item.sizeHint().height(), 82)
        self.assertFalse(page.douyin_location_results_title.isHidden())
        page._select_douyin_location_item(result_item)
        account = {
            "id": 3,
            "type": 3,
            "platformName": "抖音",
            "filePath": "oneclick_3_offline.json",
        }
        media = {"id": 8, "file_path": "offline.mp4"}
        with patch.object(page, "selected_accounts", return_value=[account]), patch.object(
            page, "selected_media", return_value=[media]
        ), patch("ui.publish_page.publish_config_service.save_tags"):
            payloads = page.collect_payloads("preflight")

        self.assertEqual(len(payloads), 1)
        self.assertTrue(payloads[0]["syncToToutiao"])
        self.assertEqual(payloads[0]["locationKeyword"], "北海银滩景区")
        self.assertEqual(payloads[0]["locationScope"], "local")
        self.assertEqual(
            payloads[0]["locationPoi"],
            {
                "poiId": "6601124346666682376",
                "name": "北海银滩景区",
                "address": "广西壮族自治区北海市银海区银滩大道中段",
                "distance": "6.0km",
            },
        )
        template = page.payload_for_template()
        self.assertEqual(template["douyinLocationKeyword"], "北海银滩景区")
        self.assertEqual(
            template["douyinLocation"]["poiId"],
            "6601124346666682376",
        )
        self.assertEqual(template["douyinLocationScope"], "local")
        page.close()

    def test_standard_douyin_location_search_is_local_only(self) -> None:
        page = PublishPage()
        account = {
            "id": 3,
            "type": 3,
            "platformName": "抖音",
            "filePath": "oneclick_3_offline.json",
        }
        page.douyin_location_keyword.setText("北海银滩")
        with patch.object(page, "_platform_accounts", return_value=[account]), patch.object(
            page.location_tasks,
            "run",
            return_value=True,
        ) as run:
            page.search_douyin_locations()

        work = run.call_args.args[1]
        with patch(
            "ui.publish_page.douyin_location_service.search_douyin_locations",
            return_value=[],
        ) as search:
            work()
        search.assert_called_once_with(account, "北海银滩", "local")
        page.close()

    def test_legacy_location_without_local_scope_is_not_restored(self) -> None:
        page = PublishPage()
        page._apply_content_payload(
            {
                "douyinLocationKeyword": "延安北巷七号社区公园",
                "douyinLocation": {
                    "poiId": "6601153104370993165",
                    "name": "延安北巷七号社区公园",
                    "address": "贵州省贵阳市云岩区延安巷27号",
                },
            }
        )
        self.assertEqual(page.douyin_location_keyword.text(), "")
        self.assertEqual(page._douyin_selected_location, {})
        self.assertIn("重新搜索", page.douyin_location_status.text())
        page.close()

    def test_plain_keyword_cannot_enter_publish_payload(self) -> None:
        page = PublishPage()
        page.common_title_input.setText("定位功能测试")
        page.title_input.setPlainText("只做离线载荷测试")
        page.douyin_location_keyword.setText("北海银滩")
        account = {
            "id": 3,
            "type": 3,
            "platformName": "抖音",
            "filePath": "oneclick_3_offline.json",
        }
        media = {"id": 8, "file_path": "offline.mp4"}
        with patch.object(page, "selected_accounts", return_value=[account]), patch.object(
            page, "selected_media", return_value=[media]
        ), patch("ui.publish_page.publish_config_service.save_tags"):
            with self.assertRaisesRegex(ValueError, "不能只填写关键词"):
                page.collect_payloads("preflight")
        page.close()


class DouyinLocationServiceTests(unittest.TestCase):
    def test_search_params_use_official_keywords_and_scope_values(self) -> None:
        domestic = douyin_location_service.location_search_params(
            "北京故宫",
            "domestic",
        )
        local = douyin_location_service.location_search_params(
            "北京故宫",
            "local",
        )

        self.assertEqual(domestic["keywords"], "北京故宫")
        self.assertNotIn("keyword", domestic)
        self.assertEqual(domestic["search_type"], "2")
        self.assertEqual(local["search_type"], "0")

    def test_commerce_editor_search_reuses_current_editor_without_raw_request(self) -> None:
        page = object()
        expected = [
            {
                "poiId": "poi-1",
                "name": "北海银滩景区",
                "address": "广西壮族自治区北海市银海区银滩大道中段",
                "distance": "",
            }
        ]
        with patch.object(
            oneclick_preflight,
            "search_douyin_location_candidates",
            new_callable=AsyncMock,
            return_value=expected,
        ) as search:
            result = asyncio.run(
                douyin_location_service.search_douyin_locations_in_editor(page, "北海")
            )

        search.assert_awaited_once_with(page, "北海")
        self.assertEqual(result, expected)

    def test_normalizes_and_deduplicates_official_poi_results(self) -> None:
        result = douyin_location_service.normalize_location_response(
            {
                "status_code": 0,
                "current_locs": [
                    {
                        "poi_id": "poi-1",
                        "poi_name": "北海银滩景区",
                        "address_info": {"simple_addr": "北海市银海区"},
                        "distance": 6120,
                    }
                ],
                "poi_list": [
                    {"poi_id": "poi-1", "poi_name": "重复项"},
                    {
                        "poi_id": "poi-2",
                        "poi_name": "北海老街",
                        "address": "北海市海城区",
                    },
                    {"poi_name": "缺少ID"},
                ],
            }
        )
        self.assertEqual([item["poiId"] for item in result], ["poi-1", "poi-2"])
        self.assertEqual(result[0]["distance"], "6.1km")
        self.assertEqual(result[0]["address"], "北海市银海区")

    def test_public_candidate_requires_complete_platform_identity(self) -> None:
        """名称、完整地址或平台 POI 标识缺一项都不能进入选择与发布。"""

        complete = {
            "poi_id": "poi-complete",
            "poi_name": "北海银滩景区",
            "address": "广西壮族自治区北海市银海区银滩大道中段",
        }
        self.assertIsNotNone(
            douyin_location_service.normalize_location_candidate(complete)
        )
        for missing_field in ("poi_id", "poi_name", "address"):
            incomplete = dict(complete)
            incomplete.pop(missing_field)
            with self.subTest(missing_field=missing_field):
                self.assertIsNone(
                    douyin_location_service.normalize_location_candidate(incomplete)
                )

    def test_addressless_rows_do_not_hide_later_complete_candidates(self) -> None:
        """缺地址候选不能占满 12 条上限，挡住后面的完整平台地点。"""

        addressless = [
            {
                "poi_id": f"poi-addressless-{index}",
                "poi_name": f"缺地址地点 {index}",
            }
            for index in range(douyin_location_service.MAX_RESULTS)
        ]
        complete = {
            "poi_id": "poi-complete",
            "poi_name": "北海银滩景区",
            "address": "广西壮族自治区北海市银海区银滩大道中段",
        }

        result = douyin_location_service.normalize_location_response(
            {
                "status_code": 0,
                "poi_list": [*addressless, complete],
            }
        )

        self.assertEqual(result, [
            {
                "poiId": "poi-complete",
                "name": "北海银滩景区",
                "address": "广西壮族自治区北海市银海区银滩大道中段",
                "distance": "",
            }
        ])

    def test_nonzero_platform_status_is_a_safe_error(self) -> None:
        with self.assertRaisesRegex(
            douyin_location_service.DouyinLocationSearchError,
            "服务繁忙",
        ):
            douyin_location_service.normalize_location_response(
                {"status_code": 1001, "status_msg": "服务繁忙"}
            )

    def test_task_validation_preserves_structured_poi(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            video = Path(temporary_directory) / "offline.mp4"
            video.write_bytes(b"offline")
            payload = {
                "type": 3,
                "contentType": "video",
                "title": "离线测试",
                "description": "离线测试",
                "fileList": [str(video)],
                "runtimeMode": "preflight",
                "debugDryRun": True,
                "locationKeyword": "北海银滩景区",
                "locationScope": "local",
                "locationPoi": {
                    "poiId": "poi-1",
                    "name": "北海银滩景区",
                    "address": "北海市银海区",
                },
            }
            validated = _validate_payloads([payload])[0]
            self.assertEqual(validated["locationPoi"]["poiId"], "poi-1")
            self.assertEqual(validated["locationPoi"]["address"], "北海市银海区")

    def test_standard_task_rejects_location_without_local_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            video = Path(temporary_directory) / "offline.mp4"
            video.write_bytes(b"offline")
            with self.assertRaisesRegex(ValueError, "仅支持账号本地地点"):
                _validate_payloads(
                    [
                        {
                            "type": 3,
                            "contentType": "video",
                            "title": "离线测试",
                            "description": "离线测试",
                            "fileList": [str(video)],
                            "runtimeMode": "preflight",
                            "debugDryRun": True,
                            "locationKeyword": "延安北巷七号社区公园",
                            "locationPoi": {
                                "poiId": "6601153104370993165",
                                "name": "延安北巷七号社区公园",
                                "address": "贵州省贵阳市云岩区延安巷27号",
                            },
                        }
                    ]
                )

    def test_task_validation_rejects_keyword_without_poi(self) -> None:
        with self.assertRaisesRegex(ValueError, "不能只传关键词"):
            _validate_payloads(
                [
                    {
                        "type": 3,
                        "contentType": "video",
                        "runtimeMode": "preflight",
                        "debugDryRun": True,
                        "locationKeyword": "北海银滩",
                    }
                ]
            )


if __name__ == "__main__":
    unittest.main()
