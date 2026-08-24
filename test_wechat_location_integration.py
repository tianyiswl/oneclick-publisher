# -*- coding: utf-8 -*-
"""公众号正文地理位置的桌面接线测试。"""

from __future__ import annotations

import inspect
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel

from app_core.oneclick_preflight import _wechat_preflight
from app_core.publish_service import _validate_payloads
from ui.publish_page import PublishPage


class WechatLocationIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_full_preflight_revalidates_location_after_image_positions(self) -> None:
        source = inspect.getsource(_wechat_preflight)
        image_readback = source.index("_wechat_verify_body_image_placements")
        location_revalidation = source.index("apply_wechat_location")
        self.assertLess(image_readback, location_revalidation)
        self.assertIn("expected_account_id", source)
        self.assertIn("正文地点已重新搜索并回读", source)

    def test_panel_is_only_visible_for_one_wechat_article_editor(self) -> None:
        page = PublishPage()
        wechat = {"id": 27, "type": 10, "filePath": "wechat-27.json"}
        douyin = {"id": 31, "type": 3, "filePath": "douyin-31.json"}
        try:
            for content_type, accounts, expected_hidden in (
                ("text", [wechat], False),
                ("article", [wechat], False),
                ("video", [wechat], True),
                ("text", [wechat, douyin], True),
                ("text", [douyin], True),
            ):
                with self.subTest(content_type=content_type, accounts=accounts):
                    page.content_type = content_type
                    with patch.object(page, "selected_accounts", return_value=accounts):
                        page._sync_wechat_location_visibility_and_context()
                    self.assertEqual(
                        page.wechat_location_panel.isHidden(),
                        expected_hidden,
                    )
        finally:
            page.close()

    def test_candidate_card_and_selection_keep_full_context(self) -> None:
        page = PublishPage()
        wechat = {"id": 27, "type": 10, "filePath": "wechat-27.json"}
        try:
            page.content_type = "text"
            page.wechat_location_keyword.setText("北海 银滩景区")
            page._show_wechat_location_results(
                [
                    {
                        "poiId": "12451333552992790600",
                        "name": "北海银滩国家旅游度假区",
                        "address": "广西壮族自治区北海市银海区潮街",
                        "latitude": "21.405443",
                        "longitude": "109.148628",
                        "platform": "wechat_article",
                    }
                ],
                source_account_id=27,
                search_keyword="北海 银滩景区",
                content_type="text",
            )
            item = page.wechat_location_results.item(0)
            card = page.wechat_location_results.itemWidget(item)
            self.assertEqual(
                card.findChild(QLabel, "wechatLocationName").text(),
                "北海银滩国家旅游度假区",
            )
            with patch.object(page, "selected_accounts", return_value=[wechat]):
                page._select_wechat_location_item(item)
            self.assertEqual(
                page._wechat_selected_location["searchKeyword"],
                "北海 银滩景区",
            )
            self.assertEqual(page._wechat_selected_location["sourceAccountId"], 27)
            self.assertEqual(
                page._wechat_selected_location["scope"],
                "article-inline-poi",
            )
        finally:
            page.close()

    def test_publish_service_keeps_only_valid_wechat_location_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cover = Path(directory) / "cover.png"
            cover.write_bytes(b"png")
            location = {
                "poiId": "12451333552992790600",
                "name": "北海银滩国家旅游度假区",
                "address": "广西壮族自治区北海市银海区潮街",
                "latitude": "21.405443",
                "longitude": "109.148628",
                "platform": "wechat_article",
                "sourceAccountId": 27,
                "platformType": 10,
                "scope": "article-inline-poi",
                "contentType": "text",
                "searchKeyword": "北海 银滩景区",
            }
            payload = {
                "type": 10,
                "contentType": "text",
                "title": "公众号地点测试",
                "description": "正文",
                "fileList": [],
                "coverPath": str(cover),
                "accountList": ["wechat-27.json"],
                "accountIds": [27],
                "runtimeMode": "preflight",
                "debugDryRun": True,
                "wechatLocationKeyword": "北海 银滩景区",
                "wechatLocationScope": "article-inline-poi",
                "wechatLocationPoi": location,
            }
            prepared = _validate_payloads([payload])[0]
            self.assertEqual(
                prepared["wechatLocationPoi"]["poiId"],
                "12451333552992790600",
            )

            unsafe = dict(payload)
            unsafe["wechatLocationKeyword"] = "北海银滩"
            with self.assertRaisesRegex(ValueError, "完整搜索词不一致"):
                _validate_payloads([unsafe])


if __name__ == "__main__":
    unittest.main()
