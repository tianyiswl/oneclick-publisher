# -*- coding: utf-8 -*-
"""一键发国内六平台内容能力矩阵的离线回归测试。"""

import unittest

from app_core import oneclick_capabilities as adapter


class OneClickCapabilityTests(unittest.TestCase):
    def test_six_platform_matrix_matches_recovered_schemas(self) -> None:
        expected_supported = {
            ("小红书", "video"), ("小红书", "article"),
            ("视频号", "video"), ("视频号", "article"),
            ("抖音", "video"), ("抖音", "article"), ("抖音", "text"),
            ("快手", "video"), ("快手", "article"),
            ("B站", "video"), ("B站", "article"), ("B站", "text"),
            ("公众号", "article"), ("公众号", "text"),
        }
        platforms = ("小红书", "视频号", "抖音", "快手", "B站", "公众号")
        content_types = ("video", "article", "text")
        actual_supported = {
            (platform, content_type)
            for platform in platforms
            for content_type in content_types
            if adapter.supports(platform, content_type)
        }
        self.assertEqual(actual_supported, expected_supported)

    def test_graphic_article_maps_to_native_platform_types(self) -> None:
        expected_native_types = {
            "小红书": "imageText", "视频号": "imageText", "抖音": "imageText",
            "快手": "imageText", "B站": "article", "公众号": "article",
        }
        for platform, native_type in expected_native_types.items():
            capability = adapter.capability_for(platform, "article")
            self.assertIsNotNone(capability)
            self.assertEqual(capability.native_publish_type, native_type)

    def test_four_overseas_platforms_only_expose_recovered_video_channel(self) -> None:
        platforms = ("TikTok", "YouTube", "Instagram Reels", "Facebook Reels")
        for platform in platforms:
            self.assertTrue(adapter.supports(platform, "video"))
            self.assertFalse(adapter.supports(platform, "article"))
            self.assertFalse(adapter.supports(platform, "text"))
        self.assertEqual(
            adapter.canonical_platform("Instagram"),
            "Instagram Reels",
        )

    def test_unsupported_type_is_blocked_before_task_creation(self) -> None:
        result = adapter.validate_payload({
            "platform": "公众号", "content_type": "video", "title": "测试视频",
            "description": "测试描述", "fileList": ["demo.mp4"],
        })
        self.assertFalse(result["valid"])
        self.assertEqual(result["nextStatus"], "不支持")
        self.assertIn("公众号 当前不支持视频发布", result["errors"][0])

    def test_cover_requirement_is_enforced_only_for_required_text_platforms(self) -> None:
        for platform in ("抖音", "公众号"):
            result = adapter.validate_payload({
                "platform": platform, "content_type": "text", "title": "纯文字测试",
                "description": "正文内容",
            })
            self.assertFalse(result["valid"])
            self.assertTrue(any("需要单独封面" in message for message in result["errors"]))

        bilibili = adapter.validate_payload({
            "platform": "B站", "content_type": "text", "title": "纯文字测试",
            "description": "正文内容",
        })
        self.assertTrue(bilibili["valid"])

    def test_valid_douyin_text_prepare_never_calls_external_service(self) -> None:
        result = adapter.prepare_task({
            "platform": "抖音", "content_type": "text", "title": "纯文字测试",
            "description": "正文内容", "coverPath": "cover.png",
        })
        self.assertTrue(result["valid"])
        self.assertEqual(result["nativePublishType"], "article")
        self.assertFalse(result["externalCall"])


if __name__ == "__main__":
    unittest.main()
