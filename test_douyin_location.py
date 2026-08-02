# -*- coding: utf-8 -*-
"""抖音发布定位的离线回归测试。"""

import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel

from app_core import douyin_location_service, oneclick_preflight
from app_core.publish_service import _validate_payloads
from ui.publish_page import PublishPage


class DouyinLocationMatchingTests(unittest.TestCase):
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
                "locationPoi": {
                    "poiId": "poi-1",
                    "name": "北海银滩景区",
                    "address": "北海市银海区",
                },
            }
            validated = _validate_payloads([payload])[0]
            self.assertEqual(validated["locationPoi"]["poiId"], "poi-1")
            self.assertEqual(validated["locationPoi"]["address"], "北海市银海区")

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
