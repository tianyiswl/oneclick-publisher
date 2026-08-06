# -*- coding: utf-8 -*-

import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from app_core.xhs_publish_executor import (
    XhsPublishError,
    _identity_name_from_response,
    _publish_button_label,
    _schedule_time,
    _validate_declaration_policy,
    _validate_payload,
)
from app_core.publish_service import _validate_payloads
from app_core.xhs_native_adapter import (
    XhsNativeAdapterError,
    _topic_candidate_matches,
    build_native_contract,
)


class XhsPublishExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.image_path = Path(self.temporary.name) / "fixture.png"
        self.image_path.write_bytes(b"offline-image-fixture")

    def tearDown(self):
        self.temporary.cleanup()

    def _payload(self):
        target = datetime.now() + timedelta(days=1)
        return {
            "type": 1,
            "contentType": "article",
            "runtimeMode": "publish",
            "debugDryRun": False,
            "enableTimer": True,
            "scheduleTime": target.strftime("%Y-%m-%d %H:%M"),
            "title": "测试标题",
            "description": "测试正文",
            "fileList": [str(self.image_path)],
        }

    def test_accepts_article_publish_mode_and_future_schedule(self):
        payload = self._payload()
        _validate_payload(payload)
        self.assertGreater(_schedule_time(payload), datetime.now())

    def test_accepts_explicit_immediate_publish(self):
        payload = self._payload()
        payload["enableTimer"] = False
        payload["scheduleTime"] = None
        _validate_payload(payload)
        self.assertIsNone(_schedule_time(payload))

    def test_rejects_schedule_value_when_timer_is_disabled(self):
        payload = self._payload()
        payload["enableTimer"] = False
        with self.assertRaisesRegex(XhsPublishError, "未开启定时"):
            _validate_payload(payload)

    def test_accepts_video_publish(self):
        payload = self._payload()
        payload["contentType"] = "video"
        video_path = Path(self.temporary.name) / "fixture.mp4"
        video_path.write_bytes(b"offline-video-fixture")
        payload["fileList"] = [str(video_path)]
        try:
            _validate_payload(payload)
        finally:
            video_path.unlink(missing_ok=True)

    def test_rejects_xhs_text_publish(self):
        payload = self._payload()
        payload["contentType"] = "text"
        with self.assertRaisesRegex(XhsPublishError, "图文或视频"):
            _validate_payload(payload)

    def test_rejects_preflight_payload(self):
        payload = self._payload()
        payload["runtimeMode"] = "preflight"
        payload["debugDryRun"] = True
        with self.assertRaisesRegex(XhsPublishError, "runtimeMode"):
            _validate_payload(payload)

    def test_reads_xhs_name_from_nested_official_identity_response(self):
        self.assertEqual(
            _identity_name_from_response(
                {"success": True, "data": {"user": {"nickname": "海风"}}}
            ),
            "海风",
        )
        self.assertIsNone(_identity_name_from_response({"data": {"id": "123"}}))

    def test_reads_publish_label_from_web_component_attribute(self):
        self.assertEqual(
            _publish_button_label(
                {
                    "tag": "XHS-PUBLISH-BTN",
                    "text": "",
                    "attrs": {
                        "submitText": "定时发布",
                        "submitDisabled": "false",
                    },
                }
            ),
            "定时发布",
        )
        self.assertEqual(
            _publish_button_label({"tag": "BUTTON", "text": "发布"}),
            "发布",
        )

    def test_ai_declaration_requires_evidence_and_authorization(self):
        payload = {
            "aiGenerated": True,
            "aiDisclosure": {
                "containsAiGeneratedContent": True,
                "allowPlatformAutoDeclaration": False,
            },
            "aiDeclarationExplicitlyConfirmed": False,
            "originalDeclaration": False,
        }
        with self.assertRaisesRegex(XhsPublishError, "未允许自动"):
            _validate_declaration_policy(payload)

        payload["aiDeclarationExplicitlyConfirmed"] = True
        _validate_declaration_policy(payload)

        payload["aiDeclarationExplicitlyConfirmed"] = False
        payload["aiDisclosure"]["allowPlatformAutoDeclaration"] = True
        _validate_declaration_policy(payload)

    def test_ai_evidence_requires_platform_declaration_confirmation(self):
        payload = {
            "aiGenerated": False,
            "aiDisclosure": {
                "containsAiGeneratedContent": True,
                "allowPlatformAutoDeclaration": False,
            },
            "originalDeclaration": False,
        }
        with self.assertRaisesRegex(XhsPublishError, "尚未确认"):
            _validate_declaration_policy(payload)

    def test_original_declaration_rejects_non_boolean_guess(self):
        with self.assertRaisesRegex(XhsPublishError, "原创声明"):
            _validate_declaration_policy({"originalDeclaration": "yes"})

    def test_native_contract_maps_local_fields_without_external_runtime(self):
        target = datetime.now() + timedelta(days=1)
        contract = build_native_contract(
            {
                "contentType": "article",
                "title": "离线映射测试",
                "description": "只验证本地字段，不访问小红书。",
                "fileList": [str(self.image_path)],
                "tags": ["测试", "#测试", "离线"],
                "enableTimer": True,
                "scheduleTime": target.strftime("%Y-%m-%d %H:%M"),
                "aiGenerated": True,
                "originalDeclaration": False,
            }
        )
        self.assertEqual(contract["formType"], "task")
        self.assertEqual(contract["visibleType"], 0)
        self.assertEqual(contract["topics"], ["测试", "离线"])
        self.assertEqual(contract["images"][0]["path"], str(self.image_path.resolve()))
        self.assertIsInstance(contract["scheduledTime"], int)
        self.assertTrue(contract["aiDeclaration"])
        self.assertEqual(
            contract["executionBackend"],
            "oneclick-official-creator-page",
        )
        self.assertFalse(contract["requiresYixiaoerClient"])
        self.assertFalse(contract["requiresYixiaoerGateway"])
        self.assertFalse(contract["usesPrivateSignatureService"])

    def test_native_contract_rejects_schema_limit_before_browser(self):
        with self.assertRaisesRegex(XhsNativeAdapterError, "标题"):
            build_native_contract(
                {
                    "contentType": "article",
                    "title": "超" * 21,
                    "description": "正文",
                    "fileList": [str(self.image_path)],
                }
            )

    def test_native_contract_rejects_more_than_ten_topics(self):
        with self.assertRaisesRegex(XhsNativeAdapterError, "10 个话题"):
            build_native_contract(
                {
                    "contentType": "article",
                    "title": "离线话题测试",
                    "description": "正文",
                    "fileList": [str(self.image_path)],
                    "tags": [f"话题{index}" for index in range(11)],
                }
            )

    def test_topic_candidate_matches_only_official_name_node(self):
        self.assertTrue(_topic_candidate_matches("#AI工具", "AI工具"))
        self.assertTrue(_topic_candidate_matches("  #AI工具\n", "#AI工具"))
        self.assertFalse(
            _topic_candidate_matches(
                "#AI工具 活动话题· 29.4亿浏览",
                "AI工具",
            )
        )
        self.assertFalse(_topic_candidate_matches("#AI工具箱", "AI工具"))

    def test_article_cover_is_first_image_without_duplication(self):
        second_image = Path(self.temporary.name) / "second.jpg"
        second_image.write_bytes(b"second-offline-image")
        contract = build_native_contract(
            {
                "contentType": "article",
                "title": "封面顺序测试",
                "description": "正文",
                "fileList": [str(second_image), str(self.image_path)],
                "coverPath": str(self.image_path),
            }
        )
        self.assertEqual(contract["coverMode"], "first-image")
        self.assertEqual(
            contract["images"][0]["path"],
            str(self.image_path.resolve()),
        )
        self.assertEqual(len(contract["images"]), 2)

    def test_video_contract_keeps_custom_cover_and_single_video(self):
        video_path = Path(self.temporary.name) / "fixture.mp4"
        video_path.write_bytes(b"offline-video-fixture")
        contract = build_native_contract(
            {
                "contentType": "video",
                "title": "视频离线测试",
                "description": "视频正文",
                "fileList": [str(video_path)],
                "coverPath": str(self.image_path),
            }
        )
        self.assertEqual(contract["nativePublishType"], "video")
        self.assertEqual(contract["video"]["path"], str(video_path.resolve()))
        self.assertEqual(contract["coverPath"], str(self.image_path.resolve()))

    def test_desktop_publish_service_accepts_xhs_immediate_video(self):
        video_path = Path(self.temporary.name) / "fixture.mp4"
        video_path.write_bytes(b"offline-video-fixture")
        payload = {
            "type": 1,
            "contentType": "video",
            "runtimeMode": "publish",
            "debugDryRun": False,
            "enableTimer": False,
            "scheduleTime": None,
            "title": "视频发布入口测试",
            "description": "只验证本地任务校验，不访问小红书。",
            "fileList": [str(video_path)],
        }
        validated = _validate_payloads([payload])
        self.assertEqual(validated[0]["contentType"], "video")

    def test_legacy_runtime_no_longer_calls_old_xhs_uploader(self):
        runtime_source = (
            Path(__file__).parent / "app_core" / "publish_runtime.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("post_video_xhs", runtime_source)
        self.assertIn("_run_oneclick_xhs", runtime_source)


if __name__ == "__main__":
    unittest.main()
