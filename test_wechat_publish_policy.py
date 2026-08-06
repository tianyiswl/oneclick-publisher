# -*- coding: utf-8 -*-
"""公众号正式发表安全策略的离线测试。"""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app_core.wechat_publish_policy import (
    build_publish_execution_record,
    decide_ai_source_declaration,
    decide_group_notification_scope_confirmation,
    decide_immediate_publish_options,
    decide_no_group_notification_confirmation,
    decide_wechat_author_readback,
    decide_wechat_publish_options,
    normalize_wechat_publish_preferences,
)


def _policy() -> dict:
    return {
        "containsAiGeneratedContent": True,
        "contentKinds": ["image"],
        "assetPaths": ["/tmp/cover.png"],
        "allowPlatformAutoDeclaration": True,
    }


def _state() -> dict:
    return {
        "dialogs": [
            {
                "text": (
                    "创作来源声明提醒 "
                    "发布内容涉及国内外时事、公共政策、社会事件或内容由AI生成等，"
                    "需对创作来源进行声明。"
                    "以下内容含有AI生成图片，平台将自动展示AI声明：测试文章"
                ),
                "buttons": ["查看", "继续发表"],
            }
        ],
        "qrElements": [],
        "qrText": [],
        "decisionMarkers": [],
    }


def _group_scope_state() -> dict:
    return {
        "dialogs": [
            {
                "title": "发表",
                "body": (
                    "已开启群发通知 "
                    "开启群发通知的内容将展示在用户的公众号列表和公众号主页。"
                    "若允许平台推荐，内容有可能被推荐至看一看或其他推荐场景。"
                ),
                "text": (
                    "发表 已开启群发通知 "
                    "开启群发通知的内容将展示在用户的公众号列表和公众号主页。"
                    "若允许平台推荐，内容有可能被推荐至看一看或其他推荐场景。"
                ),
                "buttons": ["查看详情", "继续发表", "取消"],
            }
        ],
        "qrElements": [],
        "qrText": [],
        "qrCount": 0,
    }


class WechatPublishPolicyTests(unittest.TestCase):
    def test_matching_ai_image_declaration_is_allowed(self):
        decision = decide_ai_source_declaration(_policy(), _state())
        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["expectedAction"], "继续发表")

    def test_nested_duplicate_dialog_nodes_count_as_one_visual_dialog(self):
        state = _state()
        state["dialogs"].append(dict(state["dialogs"][0]))
        decision = decide_ai_source_declaration(_policy(), state)
        self.assertTrue(decision["allowed"])

    def test_missing_manifest_flag_is_blocked(self):
        policy = _policy()
        policy["containsAiGeneratedContent"] = False
        decision = decide_ai_source_declaration(policy, _state())
        self.assertFalse(decision["allowed"])

    def test_public_policy_specific_hit_is_blocked(self):
        state = _state()
        state["dialogs"][0]["text"] = (
            "创作来源声明提醒。"
            "以下内容涉及公共政策并含有AI生成图片，平台将自动展示AI声明：测试文章"
        )
        decision = decide_ai_source_declaration(_policy(), state)
        self.assertFalse(decision["allowed"])
        self.assertIn("公共政策", decision["reason"])

    def test_qr_risk_and_extra_action_are_blocked(self):
        for key, value in (
            ("qrText", ["二维码"]),
            ("decisionMarkers", ["安全验证"]),
        ):
            state = _state()
            state[key] = value
            self.assertFalse(
                decide_ai_source_declaration(_policy(), state)["allowed"]
            )
        state = _state()
        state["dialogs"][0]["buttons"].append("确认群发")
        self.assertFalse(
            decide_ai_source_declaration(_policy(), state)["allowed"]
        )

    def test_execution_record_never_contains_session_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "oneclick_10_example.json"
            state_file.write_text('{"cookies":[{"value":"secret"}]}', encoding="utf-8")
            record = build_publish_execution_record(
                account_id=1,
                profile_name="硅基进化",
                storage_state=state_file,
                context_type="playwright-storage-state",
                editor_account_readback="硅基进化",
            )
        self.assertEqual(record["sessionSource"], "oneclick_10_example.json")
        self.assertEqual(record["editorAccountReadback"], "硅基进化")
        self.assertNotIn("cookies", record)
        self.assertNotIn("secret", str(record))

    def test_new_wechat_publish_defaults_to_group_notification_on(self):
        snapshot = {
            "options": {
                "groupNotification": {"available": True, "enabled": True},
                "groupedNotification": {"available": True, "enabled": True},
                "scheduledPublish": {"available": True, "enabled": False},
            },
            "publishButtonCount": 1,
            "buttons": ["发表", "取消"],
        }
        self.assertTrue(decide_immediate_publish_options(snapshot)["allowed"])
        snapshot["options"]["groupNotification"]["enabled"] = False
        decision = decide_immediate_publish_options(snapshot)
        self.assertFalse(decision["allowed"])
        self.assertIn("groupNotification", decision["reason"])

    def test_explicit_group_notification_off_allows_platform_dependency(self):
        snapshot = {
            "options": {
                "groupNotification": {"available": True, "enabled": False},
                "groupedNotification": {"available": False, "enabled": None},
                "scheduledPublish": {"available": True, "enabled": False},
            },
            "publishButtonCount": 1,
            "buttons": ["发表", "取消"],
        }
        decision = decide_wechat_publish_options(
            {"wechatGroupNotification": False},
            snapshot,
        )
        self.assertTrue(decision["allowed"])

        snapshot["options"]["groupedNotification"] = {
            "available": True,
            "enabled": True,
        }
        self.assertFalse(
            decide_wechat_publish_options(
                {"wechatGroupNotification": False},
                snapshot,
            )["allowed"]
        )

    def test_schedule_uses_local_timezone_and_requires_future_readback(self):
        timezone = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 7, 31, 10, 0, tzinfo=timezone)
        payload = {
            "enableTimer": True,
            "scheduleTime": "2026-08-01 18:30",
        }
        preferences = normalize_wechat_publish_preferences(
            payload,
            now=now,
            timezone=timezone,
        )
        self.assertEqual(preferences["publishMode"], "scheduled")
        self.assertEqual(preferences["timezone"], "Asia/Shanghai")
        self.assertEqual(preferences["scheduleIso"], "2026-08-01T18:30+08:00")
        snapshot = {
            "options": {
                "groupNotification": {"available": True, "enabled": True},
                "groupedNotification": {"available": True, "enabled": False},
                "scheduledPublish": {
                    "available": True,
                    "enabled": True,
                    "time": "2026-08-01 18:30",
                },
            },
            "publishButtonCount": 1,
            "buttons": ["发表", "取消"],
        }
        self.assertTrue(
            decide_wechat_publish_options(
                payload,
                snapshot,
                now=now,
                timezone=timezone,
            )["allowed"]
        )
        snapshot["options"]["scheduledPublish"]["time"] = "2026-08-01 18:31"
        self.assertFalse(
            decide_wechat_publish_options(
                payload,
                snapshot,
                now=now,
                timezone=timezone,
            )["allowed"]
        )

    def test_schedule_never_infers_timer_from_unpaired_time(self):
        with self.assertRaises(ValueError):
            normalize_wechat_publish_preferences(
                {"enableTimer": False, "scheduleTime": "2099-01-01 10:00"}
            )
        with self.assertRaises(ValueError):
            normalize_wechat_publish_preferences(
                {
                    "enableTimer": True,
                    "scheduleTime": "2026-07-31 09:00",
                },
                now=datetime(
                    2026,
                    7,
                    31,
                    10,
                    0,
                    tzinfo=ZoneInfo("Asia/Shanghai"),
                ),
                timezone=ZoneInfo("Asia/Shanghai"),
            )

    def test_author_is_completely_skipped_without_original_declaration(self):
        decision = decide_wechat_author_readback(
            {"originalDeclaration": False},
            {"authorControlTouched": False},
        )
        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["authorPolicy"], "skip-completely")
        self.assertFalse(
            decide_wechat_author_readback(
                {"originalDeclaration": False},
                {"authorControlTouched": True, "selectedName": "作者甲"},
            )["allowed"]
        )

    def test_original_selects_first_available_author_and_requires_readback(self):
        payload = {"originalDeclaration": True}
        self.assertTrue(
            decide_wechat_author_readback(
                payload,
                {
                    "authorControlTouched": True,
                    "firstAvailableName": "作者甲",
                    "selectedName": "作者甲",
                },
            )["allowed"]
        )
        self.assertFalse(
            decide_wechat_author_readback(
                payload,
                {
                    "authorControlTouched": True,
                    "firstAvailableName": "作者甲",
                    "selectedName": "作者乙",
                },
            )["allowed"]
        )

    def test_only_exact_no_group_notification_confirmation_is_allowed(self):
        state = {
            "dialogs": [
                {
                    "text": (
                        "发表 未开启群发通知 内容将展示在公众号主页，"
                        "若允许平台推荐，内容有可能被推荐至看一看或其他推荐场景。"
                    ),
                    "buttons": ["查看详情", "继续发表", "取消"],
                }
            ],
            "qrElements": [],
            "qrText": [],
            "decisionMarkers": [],
        }
        state["dialogs"].append(dict(state["dialogs"][0]))
        self.assertTrue(
            decide_no_group_notification_confirmation(state)["allowed"]
        )
        state["dialogs"][0]["text"] += "需要管理员验证"
        state["dialogs"][1] = dict(state["dialogs"][0])
        self.assertFalse(
            decide_no_group_notification_confirmation(state)["allowed"]
        )

    def test_exact_group_notification_scope_confirmation_is_allowed(self):
        state = _group_scope_state()
        state["dialogs"].append(dict(state["dialogs"][0]))
        decision = decide_group_notification_scope_confirmation(
            {"wechatGroupNotification": True},
            state,
        )
        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["expectedAction"], "继续发表")

    def test_group_notification_scope_requires_enabled_group_notification(self):
        decision = decide_group_notification_scope_confirmation(
            {"wechatGroupNotification": False},
            _group_scope_state(),
        )
        self.assertFalse(decision["allowed"])
        self.assertIn("未开启", decision["reason"])

    def test_group_notification_scope_blocks_changed_copy_or_buttons(self):
        state = _group_scope_state()
        state["dialogs"][0]["body"] += "还需同意新的平台协议。"
        self.assertFalse(
            decide_group_notification_scope_confirmation(
                {"wechatGroupNotification": True},
                state,
            )["allowed"]
        )

        state = _group_scope_state()
        state["dialogs"][0]["buttons"].append("同意协议")
        self.assertFalse(
            decide_group_notification_scope_confirmation(
                {"wechatGroupNotification": True},
                state,
            )["allowed"]
        )

    def test_group_notification_scope_never_auto_accepts_qr(self):
        state = _group_scope_state()
        state["qrCount"] = 1
        state["qrText"] = ["二维码"]
        decision = decide_group_notification_scope_confirmation(
            {"wechatGroupNotification": True},
            state,
        )
        self.assertFalse(decision["allowed"])
        self.assertIn("二维码", decision["reason"])


if __name__ == "__main__":
    unittest.main()
