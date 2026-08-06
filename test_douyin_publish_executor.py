# -*- coding: utf-8 -*-
"""抖音正式发布执行器的离线安全回归测试。"""

from __future__ import annotations

from datetime import datetime, timedelta
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app_core import douyin_publish_executor

try:
    from app_core import publish_service
except ModuleNotFoundError:
    # 纯离线校验环境没有 Playwright 时，仍执行所有不依赖浏览器的安全规则；
    # 完整开发依赖环境会自动执行下面的路由测试。
    publish_service = None


class DouyinPublishPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.video = root / "demo.mp4"
        self.cover = root / "cover.jpg"
        self.video.write_bytes(b"demo-video")
        self.cover.write_bytes(b"demo-cover")
        self.payload = {
            "type": 3,
            "runtimeMode": "publish",
            "debugDryRun": False,
            "contentType": "video",
            "title": "定位正式发布离线测试",
            "description": "只验证本地路由与 POI 规则。",
            "fileList": [str(self.video)],
            "accountList": ["oneclick_3_offline.json"],
            "coverPath": str(self.cover),
            "coverPaths": {"3:4": str(self.cover)},
            "locationKeyword": "北海银滩景区",
            "locationPoi": {
                "poiId": "6601124346666682376",
                "name": "北海银滩景区",
                "address": "广西壮族自治区北海市银海区银滩大道中段(4号路)",
                "distance": "",
            },
            "enableTimer": False,
            "backgroundMode": True,
        }

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_normalizes_structured_poi_and_forces_foreground(self) -> None:
        checked = douyin_publish_executor.validate_douyin_publish_payload(self.payload)
        self.assertFalse(checked["backgroundMode"])
        self.assertEqual(checked["locationPoi"]["poiId"], "6601124346666682376")
        self.assertEqual(checked["locationKeyword"], "北海银滩景区")
        self.assertIsNone(checked["scheduleTime"])

    def test_rejects_plain_location_keyword(self) -> None:
        payload = dict(self.payload)
        payload["locationPoi"] = {}
        with self.assertRaisesRegex(
            douyin_publish_executor.DouyinPublishError,
            "不能只传关键词",
        ):
            douyin_publish_executor.validate_douyin_publish_payload(payload)

    def test_rejects_multiple_targets_before_browser_start(self) -> None:
        payload = dict(self.payload)
        payload["accountList"] = ["a.json", "b.json"]
        with self.assertRaisesRegex(
            douyin_publish_executor.DouyinPublishError,
            "只能选择一个",
        ):
            douyin_publish_executor.validate_douyin_publish_payload(payload)

    def test_normalizes_future_scheduling_without_falling_back_to_immediate(self) -> None:
        payload = dict(self.payload)
        payload["enableTimer"] = True
        expected = datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=1)
        payload["scheduleTime"] = expected.strftime("%Y-%m-%d %H:%M")
        checked = douyin_publish_executor.validate_douyin_publish_payload(payload)
        self.assertTrue(checked["enableTimer"])
        self.assertEqual(checked["scheduleTime"], payload["scheduleTime"])

    def test_rejects_schedule_time_without_enabled_switch(self) -> None:
        payload = dict(self.payload)
        expected = datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=1)
        payload["scheduleTime"] = expected.strftime("%Y-%m-%d %H:%M")
        with self.assertRaisesRegex(
            douyin_publish_executor.DouyinPublishError,
            "未开启抖音定时发布",
        ):
            douyin_publish_executor.validate_douyin_publish_payload(payload)

    def test_scheduled_readback_requires_exact_time_and_a_date_variant(self) -> None:
        target = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None) + timedelta(days=1)
        display = target.strftime("%Y年%m月%d日 %H:%M")
        self.assertTrue(
            douyin_publish_executor._schedule_text_matches(
                f"随便的自然英文表达 定时发布 {display}", target
            )
        )
        wrong_time = (target + timedelta(minutes=1)).strftime("%H:%M")
        self.assertFalse(
            douyin_publish_executor._schedule_text_matches(
                f"随便的自然英文表达 {target.strftime('%Y-%m-%d')} {wrong_time}",
                target,
            )
        )

    def test_official_identity_must_exactly_match_selected_account(self) -> None:
        self.assertEqual(
            douyin_publish_executor._verified_douyin_identity("知言", " 知言 "),
            "知言",
        )
        with self.assertRaisesRegex(
            douyin_publish_executor.DouyinPublishError,
            "不一致",
        ):
            douyin_publish_executor._verified_douyin_identity("知言", "其他账号")
        with self.assertRaisesRegex(
            douyin_publish_executor.DouyinPublishError,
            "未包含可用账号名",
        ):
            douyin_publish_executor._verified_douyin_identity("知言", "")

    def test_only_final_confirmation_failures_hold_foreground(self) -> None:
        self.assertTrue(
            douyin_publish_executor._requires_foreground_hold(
                "抖音点击发布后未确认跳转"
            )
        )
        self.assertTrue(
            douyin_publish_executor._requires_foreground_hold(
                "抖音正在等待二次安全验证"
            )
        )
        self.assertFalse(
            douyin_publish_executor._requires_foreground_hold(
                "抖音发布定位未能唯一确认"
            )
        )
        self.assertFalse(
            douyin_publish_executor._requires_foreground_hold(
                "抖音收藏音乐未能唯一选择并回读：页面未找到唯一可用的添加音乐控件"
            )
        )
        self.assertTrue(
            douyin_publish_executor._requires_foreground_hold(
                "抖音页面要求扫码登录后继续"
            )
        )


@unittest.skipIf(publish_service is None, "当前离线环境未安装 Playwright，跳过桌面路由测试")
class DouyinPublishRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.video = root / "demo.mp4"
        self.cover = root / "cover.jpg"
        self.video.write_bytes(b"demo-video")
        self.cover.write_bytes(b"demo-cover")
        self.payload = {
            "type": 3,
            "runtimeMode": "publish",
            "debugDryRun": False,
            "contentType": "video",
            "title": "抖音正式路由离线测试",
            "description": "验证一键发正式路由。",
            "fileList": [str(self.video)],
            "accountList": ["oneclick_3_offline.json"],
            "coverPath": str(self.cover),
            "coverPaths": {"3:4": str(self.cover)},
            "locationKeyword": "",
            "locationPoi": {},
            "enableTimer": False,
        }

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_publish_service_routes_douyin_to_controlled_executor(self) -> None:
        prepared = publish_service._validate_payloads([self.payload])
        with patch.object(
            publish_service.douyin_publish_executor,
            "run_douyin_publish_sync",
            return_value={"ok": True, "message": "平台管理页已回读"},
        ) as execute, patch.object(
            publish_service.task_service, "mark_task_running"
        ), patch.object(
            publish_service.task_service, "record_task_event"
        ), patch.object(
            publish_service.task_service, "mark_platform_result"
        ) as mark:
            publish_service._run_publish({"id": 301}, prepared)

        execute.assert_called_once_with(prepared[0], task_id=301)
        self.assertTrue(mark.call_args.kwargs["ok"])


if __name__ == "__main__":
    unittest.main()
