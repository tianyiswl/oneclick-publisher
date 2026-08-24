# -*- coding: utf-8 -*-
"""视频号视频定位的桌面接线测试。"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel

from app_core.publish_service import _validate_payloads
from app_core.oneclick_preflight import _video_channel_video_preflight
from ui.publish_page import PublishPage


def _selection() -> dict[str, object]:
    return {
        "poiId": "qqmap_11122499332116978481",
        "name": "长青公园",
        "address": "广西壮族自治区北海市海城区北京路以东,北海大道以北",
        "longitude": 109.12338256835938,
        "latitude": 21.472030639648438,
        "poiCheckSum": "980e94c1117e5231f85083983c52517f",
        "platform": "video-channel",
        "sourceAccountId": 5,
        "platformType": 2,
        "scope": "platform-default",
        "contentType": "video",
        "searchKeyword": "长青公园",
    }


class _PreflightLocator:
    def __init__(self) -> None:
        self.value = ""
        self.uploaded = ""

    @property
    def first(self):
        return self

    @property
    def last(self):
        return self

    async def count(self) -> int:
        return 1

    async def click(self, **_kwargs) -> None:
        return None

    async def wait_for(self, **_kwargs) -> None:
        return None

    async def set_input_files(self, value: str) -> None:
        self.uploaded = value

    async def evaluate(self, _script: str, value: str) -> None:
        self.value = value

    async def inner_text(self) -> str:
        return self.value

    async def input_value(self) -> str:
        return self.value


class _PreflightFrame:
    def __init__(self) -> None:
        self.description = _PreflightLocator()
        self.title = _PreflightLocator()

    def locator(self, selector: str):
        if selector == "div.input-editor":
            return self.description
        if "短标题" in selector:
            return self.title
        return _PreflightLocator()


class _PreflightPage:
    def __init__(self) -> None:
        self.entry = _PreflightLocator()
        self.upload = _PreflightLocator()
        self.frame = _PreflightFrame()
        self.frames = [self.frame]

    async def goto(self, *_args, **_kwargs) -> None:
        return None

    async def wait_for_timeout(self, _milliseconds: int) -> None:
        return None

    def get_by_text(self, *_args, **_kwargs):
        return self.entry

    def locator(self, selector: str):
        if selector == "input[type=file]":
            return self.upload
        return _PreflightLocator()


class VideoChannelLocationIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_panel_is_only_visible_for_one_video_channel_video_account(self) -> None:
        page = PublishPage()
        video_channel = {"id": 5, "type": 2, "filePath": "video-channel.json"}
        douyin = {"id": 8, "type": 3, "filePath": "douyin.json"}
        try:
            for content_type, accounts, expected_hidden in (
                ("video", [video_channel], False),
                ("article", [video_channel], True),
                ("video", [video_channel, douyin], True),
                ("video", [douyin], True),
            ):
                with self.subTest(content_type=content_type, accounts=accounts):
                    page.content_type = content_type
                    with patch.object(page, "selected_accounts", return_value=accounts):
                        page._sync_video_channel_location_visibility_and_context()
                    self.assertEqual(
                        page.video_channel_location_panel.isHidden(),
                        expected_hidden,
                    )
        finally:
            page.close()

    def test_candidate_card_and_selection_keep_full_platform_context(self) -> None:
        page = PublishPage()
        account = {"id": 5, "type": 2, "filePath": "video-channel.json"}
        try:
            page.content_type = "video"
            page.video_channel_location_keyword.setText("长青公园")
            page._show_video_channel_location_results(
                [_selection()],
                source_account_id=5,
                search_keyword="长青公园",
                content_type="video",
            )
            item = page.video_channel_location_results.item(0)
            card = page.video_channel_location_results.itemWidget(item)
            self.assertEqual(
                card.findChild(QLabel, "videoChannelLocationName").text(),
                "长青公园",
            )
            self.assertEqual(
                card.findChild(QLabel, "videoChannelLocationAddress").text(),
                "广西壮族自治区北海市海城区北京路以东,北海大道以北",
            )
            with patch.object(page, "selected_accounts", return_value=[account]):
                page._select_video_channel_location_item(item)
            self.assertEqual(
                page._video_channel_selected_location,
                _selection(),
            )
        finally:
            page.close()

    def test_publish_service_keeps_only_valid_video_channel_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "probe.mp4"
            video.write_bytes(b"video")
            payload = {
                "type": 2,
                "contentType": "video",
                "title": "定位功能预检",
                "description": "只检查位置，不保存不发布。",
                "fileList": [str(video)],
                "accountList": ["video-channel.json"],
                "accountIds": [5],
                "runtimeMode": "preflight",
                "debugDryRun": True,
                "videoChannelLocationKeyword": "长青公园",
                "videoChannelLocationScope": "platform-default",
                "videoChannelLocationPoi": _selection(),
            }

            prepared = _validate_payloads([payload])[0]
            self.assertEqual(
                prepared["videoChannelLocationPoi"]["poiId"],
                "qqmap_11122499332116978481",
            )

            unsafe = dict(payload)
            unsafe["videoChannelLocationKeyword"] = "北部湾广场"
            with self.assertRaisesRegex(ValueError, "搜索词"):
                _validate_payloads([unsafe])

    def test_video_preflight_revalidates_location_before_returning_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "probe.mp4"
            video.write_bytes(b"video")
            payload = {
                "type": 2,
                "contentType": "video",
                "title": "定位功能预检",
                "description": "只检查位置，不保存不发布。",
                "fileList": [str(video)],
                "accountIds": [5],
                "videoChannelLocationKeyword": "长青公园",
                "videoChannelLocationScope": "platform-default",
                "videoChannelLocationPoi": _selection(),
            }
            with patch(
                "app_core.video_channel_location_service.apply_video_channel_location",
                new_callable=AsyncMock,
                return_value=_selection(),
            ):
                result = asyncio.run(
                    _video_channel_video_preflight(_PreflightPage(), payload)
                )
            self.assertIn("位置已重新搜索并回读：长青公园", result)


if __name__ == "__main__":
    unittest.main()
