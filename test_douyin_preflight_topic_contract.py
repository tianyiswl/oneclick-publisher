# -*- coding: utf-8 -*-
"""抖音预检必须复用正式阶段的话题选择与回读合同。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app_core.oneclick_preflight import _douyin_video_preflight


class _Upload:
    async def wait_for(self, **_kwargs):
        return None

    async def set_input_files(self, _path):
        return None


class _Locator:
    @property
    def first(self):
        return _Upload()


class _Page:
    def locator(self, _selector):
        return _Locator()

    async def goto(self, *_args, **_kwargs):
        return None

    async def wait_for_url(self, *_args, **_kwargs):
        return None


class DouyinPreflightTopicContractTests(unittest.TestCase):
    def test_preflight_calls_formal_editor_topic_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "video.mp4"
            video.write_bytes(b"video")
            uploader = MagicMock()
            uploader.sync_uploaded_editor_content = AsyncMock(
                return_value={"tags": ["AI工具", "个人项目"]}
            )
            uploader_type = MagicMock(return_value=uploader)
            with patch(
                "uploader.douyin_uploader.main.DouYinVideo", uploader_type
            ), patch(
                "app_core.oneclick_preflight._douyin_set_location",
                new_callable=AsyncMock,
                return_value="",
            ):
                result = asyncio.run(
                    _douyin_video_preflight(
                        _Page(),
                        {
                            "fileList": [str(video)],
                            "title": "测试标题",
                            "description": "测试正文",
                            "tags": ["AI工具", "个人项目"],
                            "aiGenerated": False,
                        },
                    )
                )

        uploader.sync_uploaded_editor_content.assert_awaited_once()
        self.assertEqual(
            uploader.sync_uploaded_editor_content.await_args.kwargs["tags"],
            ["AI工具", "个人项目"],
        )
        self.assertIn("2 个平台话题", result)


if __name__ == "__main__":
    unittest.main()
