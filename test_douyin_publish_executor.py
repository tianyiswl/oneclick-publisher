# -*- coding: utf-8 -*-
"""抖音正式发布执行器的离线安全回归测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from io import BytesIO
import tempfile
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from loguru import logger
import qrcode
from PIL import Image

from app_core import douyin_publish_executor
from app_core.douyin_verification import VerificationChallenge, verification_broker
from uploader.douyin_uploader.main import DouYinVideo, TopicCandidateUnavailable

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

    def test_standard_publish_rejects_location_without_complete_address(self) -> None:
        """普通发布的严格地点规则不能依赖带货共享解析器。"""

        payload = dict(self.payload)
        payload["locationPoi"] = {
            "poiId": "6601124346666682376",
            "name": "北海银滩景区",
        }

        with self.assertRaisesRegex(
            douyin_publish_executor.DouyinPublishError,
            "不能只传关键词",
        ):
            douyin_publish_executor.validate_douyin_publish_payload(payload)

    def test_normalizes_only_explicit_ai_generated_boolean(self) -> None:
        payload = dict(self.payload)
        payload["aiGenerated"] = True
        checked = douyin_publish_executor.validate_douyin_publish_payload(payload)
        self.assertTrue(checked["aiGenerated"])

        payload["aiGenerated"] = "true"
        checked = douyin_publish_executor.validate_douyin_publish_payload(payload)
        self.assertFalse(checked["aiGenerated"])

    def test_standard_publish_sms_verification_uses_native_broker(self) -> None:
        """标准发布应在原会话等待客户端验证码，不要求用户操作浏览器。"""

        class Page:
            url = "https://creator.douyin.com/verification"

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                await asyncio.sleep(0)

        class Uploader:
            def __init__(self) -> None:
                self.codes: list[str] = []

            async def apply_sms_verification_code(
                self,
                _page,
                _challenge,
                code: str,
                before_submit=None,
            ) -> None:
                if before_submit:
                    before_submit()
                self.codes.append(code)

            async def resend_sms_verification_code(self, _page, _challenge) -> None:
                return None

        async def scenario() -> Uploader:
            uploader = Uploader()
            task = asyncio.create_task(
                douyin_publish_executor._handle_publish_verification(
                    uploader,
                    Page(),
                    VerificationChallenge(kind="sms", message=""),
                    task_id=90210,
                )
            )
            request_id = None
            for _ in range(20):
                await asyncio.sleep(0)
                request_id = verification_broker.request_for_task(90210)
                if request_id:
                    break
            self.assertIsNotNone(request_id)
            verification_broker.submit_code(str(request_id), "123456")
            await task
            self.assertIsNone(verification_broker.request_for_task(90210))
            return uploader

        with patch.object(
            douyin_publish_executor.task_service,
            "record_task_event",
        ) as record:
            uploader = asyncio.run(scenario())

        self.assertEqual(uploader.codes, ["123456"])
        self.assertEqual(record.call_args.args[1], "douyin_verification_required")

    def test_standard_publish_qr_verification_waits_for_management_receipt(self) -> None:
        """扫码完成后只有进入作品管理页才可继续，不能凭弹层消失猜成功。"""

        qr_output = BytesIO()
        qrcode.make("standard-publish-verification").save(qr_output, format="PNG")

        class Page:
            url = "https://creator.douyin.com/verification"

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                await asyncio.sleep(0)

        class Uploader:
            async def detect_publish_verification(self, page):
                if "/creator-micro/content/manage" in page.url:
                    return None
                return VerificationChallenge(
                    kind="qr",
                    message="",
                    qr_image=qr_output.getvalue(),
                )

        async def scenario() -> None:
            page = Page()
            task = asyncio.create_task(
                douyin_publish_executor._handle_publish_verification(
                    Uploader(),
                    page,
                    VerificationChallenge(
                        kind="qr",
                        message="",
                        qr_image=qr_output.getvalue(),
                    ),
                    task_id=90212,
                )
            )
            request_id = None
            for _ in range(20):
                await asyncio.sleep(0)
                request_id = verification_broker.request_for_task(90212)
                if request_id:
                    break
            self.assertIsNotNone(request_id)
            page.url = "https://creator.douyin.com/creator-micro/content/manage"
            await task
            self.assertIsNone(verification_broker.request_for_task(90212))

        with patch.object(
            douyin_publish_executor.task_service,
            "record_task_event",
        ) as record:
            asyncio.run(scenario())

        self.assertEqual(record.call_args.args[1], "douyin_verification_required")

    def test_uploader_forwards_injected_verification_callback(self) -> None:
        """上传器点击发布后必须把标准发布协调器交给验证等待器。"""

        video = DouYinVideo(
            title="测试",
            file_path=str(self.video),
            tags=[],
            publish_date=0,
            account_file="offline.json",
            description="测试",
        )
        callback = AsyncMock()
        video.verification_callback = callback
        video.external_page = MagicMock()
        video.external_context = MagicMock()
        video.external_browser = MagicMock()
        publish_button = MagicMock()
        publish_button.click = AsyncMock()

        async_methods = (
            "prepare_uploaded_video_editor",
            "set_favorite_music",
            "set_collection",
            "set_structured_location",
            "set_commerce_store",
            "set_ai_generated_declaration",
        )
        patches = [
            patch.object(video, name, new_callable=AsyncMock)
            for name in async_methods
        ]
        started = [item.start() for item in patches]
        del started
        try:
            with patch.object(
                video,
                "wait_publish_button_ready",
                new_callable=AsyncMock,
                return_value=publish_button,
            ), patch.object(
                video,
                "_wait_formal_publish_result",
                new_callable=AsyncMock,
                return_value={"status": "published"},
            ) as wait_result, patch(
                "uploader.douyin_uploader.main.save_context_storage_state",
                new_callable=AsyncMock,
            ):
                receipt = asyncio.run(video.upload(MagicMock()))
        finally:
            for item in reversed(patches):
                item.stop()

        self.assertEqual(receipt["status"], "published")
        self.assertIs(
            wait_result.await_args.kwargs["on_verification"],
            callback,
        )

    def test_topic_contract_retries_whole_set_after_transient_empty_candidates(self) -> None:
        video = DouYinVideo(
            title="测试",
            file_path=str(self.video),
            tags=["AI工具", "个人项目"],
            publish_date=0,
            account_file="offline.json",
            description="正文",
        )
        add_topic = AsyncMock(
            side_effect=[
                TopicCandidateUnavailable("抖音未返回话题“AI工具”的精确平台候选"),
                None,
                None,
            ]
        )
        with patch.object(video, "_add_platform_topic", add_topic), patch.object(
            video, "_fill_editor_body", new_callable=AsyncMock
        ) as refill, patch.object(
            video,
            "_wait_platform_topics_stable",
            new_callable=AsyncMock,
            return_value=["AI工具", "个人项目"],
        ):
            page = MagicMock()
            page.wait_for_timeout = AsyncMock()
            confirmed, missing = asyncio.run(
                video._apply_platform_topics(
                    page, MagicMock(), "正文", ["AI工具", "个人项目"]
                )
            )

        self.assertEqual(confirmed, ["AI工具", "个人项目"])
        self.assertEqual(missing, [])
        self.assertEqual(add_topic.await_count, 3)
        refill.assert_awaited_once()

    def test_topic_contract_rebuilds_when_last_topic_disappears_after_first_readback(self) -> None:
        video = DouYinVideo(
            title="测试",
            file_path=str(self.video),
            tags=["AI工具", "抖音前沿科技首发计划"],
            publish_date=0,
            account_file="offline.json",
            description="正文",
        )
        delayed_drop = TopicCandidateUnavailable(
            "抖音平台话题稳定回读不一致：期望=['AI工具', '抖音前沿科技首发计划']，"
            "实际=['AI工具']"
        )
        with patch.object(
            video, "_add_platform_topic", new_callable=AsyncMock
        ) as add_topic, patch.object(
            video, "_fill_editor_body", new_callable=AsyncMock
        ) as refill, patch.object(
            video,
            "_wait_platform_topics_stable",
            new_callable=AsyncMock,
            side_effect=[delayed_drop, ["AI工具", "抖音前沿科技首发计划"]],
        ) as stable_readback:
            page = MagicMock()
            page.wait_for_timeout = AsyncMock()
            confirmed, missing = asyncio.run(
                video._apply_platform_topics(
                    page,
                    MagicMock(),
                    "正文",
                    ["AI工具", "抖音前沿科技首发计划"],
                )
            )

        self.assertEqual(confirmed, ["AI工具", "抖音前沿科技首发计划"])
        self.assertEqual(missing, [])
        self.assertEqual(add_topic.await_count, 4)
        self.assertEqual(stable_readback.await_count, 2)
        refill.assert_awaited_once()

    def test_topic_contract_reports_delayed_missing_topic_after_both_rounds(self) -> None:
        video = DouYinVideo(
            title="测试",
            file_path=str(self.video),
            tags=["AI工具", "抖音前沿科技首发计划"],
            publish_date=0,
            account_file="offline.json",
            description="正文",
        )
        delayed_drop = TopicCandidateUnavailable("稳定回读丢失最后一个话题")
        delayed_drop.missing_topics = ["抖音前沿科技首发计划"]
        with patch.object(
            video, "_add_platform_topic", new_callable=AsyncMock
        ), patch.object(
            video, "_fill_editor_body", new_callable=AsyncMock
        ), patch.object(
            video,
            "_wait_platform_topics_stable",
            new_callable=AsyncMock,
            side_effect=[delayed_drop, delayed_drop],
        ):
            page = MagicMock()
            page.wait_for_timeout = AsyncMock()
            confirmed, missing = asyncio.run(
                video._apply_platform_topics(
                    page,
                    MagicMock(),
                    "正文",
                    ["AI工具", "抖音前沿科技首发计划"],
                )
            )

        self.assertEqual(confirmed, [])
        self.assertEqual(missing, ["抖音前沿科技首发计划"])

    def test_topic_stability_rejects_single_success_followed_by_delayed_drop(self) -> None:
        video = DouYinVideo(
            title="测试",
            file_path=str(self.video),
            tags=["AI工具", "抖音前沿科技首发计划"],
            publish_date=0,
            account_file="offline.json",
            description="正文",
        )
        page = MagicMock()
        page.wait_for_timeout = AsyncMock()
        with patch.object(
            video,
            "_read_platform_topics",
            new_callable=AsyncMock,
            side_effect=[
                ["AI工具", "抖音前沿科技首发计划"],
                ["AI工具"],
                ["AI工具"],
                ["AI工具"],
                ["AI工具"],
                ["AI工具"],
            ],
        ):
            with self.assertRaises(TopicCandidateUnavailable) as raised:
                asyncio.run(
                    video._wait_platform_topics_stable(
                        page,
                        MagicMock(),
                        ["AI工具", "抖音前沿科技首发计划"],
                        attempts=6,
                        stable_reads=3,
                    )
                )

        self.assertIn("实际=['AI工具']", str(raised.exception))
        self.assertEqual(
            raised.exception.error_code,
            "douyin_topic_candidates_unavailable",
        )

    def test_topic_contract_returns_stable_error_after_bounded_retries(self) -> None:
        video = DouYinVideo(
            title="测试",
            file_path=str(self.video),
            tags=["AI工具"],
            publish_date=0,
            account_file="offline.json",
            description="正文",
        )
        unavailable = TopicCandidateUnavailable(
            "抖音未返回话题“AI工具”的精确平台候选"
        )
        with patch.object(
            video,
            "_add_platform_topic",
            new_callable=AsyncMock,
            side_effect=[unavailable, unavailable],
        ), patch.object(video, "_fill_editor_body", new_callable=AsyncMock):
            page = MagicMock()
            page.wait_for_timeout = AsyncMock()
            confirmed, missing = asyncio.run(
                video._apply_platform_topics(
                    page, MagicMock(), "正文", ["AI工具"]
                )
            )

        self.assertEqual(confirmed, [])
        self.assertEqual(missing, ["AI工具"])
        self.assertEqual(
            unavailable.error_code, "douyin_topic_candidates_unavailable"
        )

    def test_topic_entry_uses_platform_toolbar_and_exact_text_readback(self) -> None:
        """新版抖音只通过“#添加话题”写入文本，不得依赖旧候选弹层。"""

        video = DouYinVideo(
            title="测试",
            file_path=str(self.video),
            tags=["硅基探索"],
            publish_date=0,
            account_file="offline.json",
            description="正文",
        )
        control = MagicMock()
        control.click = AsyncMock()
        editor = MagicMock()
        editor.press_sequentially = AsyncMock()
        editor.press = AsyncMock()
        page = MagicMock()
        page.get_by_text = MagicMock(return_value=MagicMock())
        page.wait_for_timeout = AsyncMock()

        with patch.object(
            video,
            "_visible_enabled_items",
            new_callable=AsyncMock,
            return_value=[control],
        ), patch.object(
            video,
            "_read_platform_topics",
            new_callable=AsyncMock,
            side_effect=[[], ["硅基探索"]],
        ), patch.object(
            video,
            "_find_unique_topic_candidate",
            new_callable=AsyncMock,
        ) as old_candidate_lookup:
            asyncio.run(video._add_platform_topic(page, editor, "硅基探索"))

        control.click.assert_awaited_once()
        editor.press_sequentially.assert_awaited_once_with("硅基探索", delay=50)
        editor.press.assert_awaited_once_with("Space")
        old_candidate_lookup.assert_not_awaited()

    def test_topic_readback_preserves_mixed_raw_and_component_dom_order(self) -> None:
        """原始话题和自动转换组件混合时，回读顺序必须与页面一致。"""

        video = DouYinVideo(
            title="测试",
            file_path=str(self.video),
            tags=["硅基探索", "AI开发"],
            publish_date=0,
            account_file="offline.json",
            description="正文",
        )
        raw_node = MagicMock()
        raw_node.get_attribute = AsyncMock(return_value=None)
        raw_node.inner_text = AsyncMock(return_value="正文#硅基探索 ")
        mention_node = MagicMock()
        mention_node.get_attribute = AsyncMock(return_value="#")
        mention_node.inner_text = AsyncMock(return_value=" #AI开发 ")
        nodes = MagicMock()
        nodes.count = AsyncMock(return_value=2)
        nodes.nth = MagicMock(side_effect=[raw_node, mention_node])
        editor = MagicMock()
        editor.locator.return_value = nodes

        topics = asyncio.run(video._read_platform_topics(editor))

        self.assertEqual(topics, ["硅基探索", "AI开发"])

    def test_verify_form_accepts_exact_toolbar_topic_text_and_keeps_body_clean(self) -> None:
        video = DouYinVideo(
            title="测试标题",
            file_path=str(self.video),
            tags=["AI工具", "个人项目"],
            publish_date=0,
            account_file="offline.json",
            description="测试正文",
        )
        editor = MagicMock()
        editor.wait_for = AsyncMock()
        page = MagicMock()
        page.locator.return_value.first = editor
        title_input = MagicMock()
        title_input.input_value = AsyncMock(return_value="测试标题")

        with patch.object(
            video,
            "_read_raw_editor_text",
            new_callable=AsyncMock,
            return_value="测试正文#AI工具 #个人项目 ",
        ), patch.object(
            video,
            "_read_platform_topics",
            new_callable=AsyncMock,
            return_value=["AI工具", "个人项目"],
        ), patch.object(
            video,
            "_visible_title_inputs",
            new_callable=AsyncMock,
            return_value=[title_input],
        ):
            result = asyncio.run(video.verify_prepublish_form(page, require_covers=False))

        self.assertEqual(result["topics_confirmed"], ["AI工具", "个人项目"])
        self.assertEqual(result["topic_entry_method"], "platform_toolbar_exact_text_readback")

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

    def test_scheduled_readback_scrolls_lazy_list_until_same_card_matches(self) -> None:
        """管理页按定时时间排序且懒加载时，首屏之外的真实作品也必须能回读。"""

        target = datetime(2026, 8, 9, 10, 0)

        class Body:
            async def inner_text(self, *, timeout: int) -> str:
                del timeout
                return "作品管理 首屏只有其他作品"

        class Page:
            url = "https://creator.douyin.com/creator-micro/content/manage"

            def __init__(self) -> None:
                self.scan_count = 0
                self.scroll_count = 0

            def is_closed(self) -> bool:
                return False

            def locator(self, selector: str):
                self.assert_selector = selector
                return Body()

            async def evaluate(self, script: str, payload=None):
                if "oneclick-scheduled-card-scan" in script:
                    self.scan_count += 1
                    if self.scroll_count:
                        return [
                            "测试 测试#测试 定时发布中 "
                            "定时: 2026年08月09日 10:00 修改定时"
                        ]
                    return []
                if "oneclick-scheduled-list-scroll" in script:
                    self.scroll_count += 1
                    return True
                raise AssertionError(f"未知脚本：{script[:80]}")

            async def wait_for_timeout(self, milliseconds: int) -> None:
                del milliseconds

            async def reload(self, **_kwargs) -> None:
                raise AssertionError("两次内可回读时不应刷新管理页")

        page = Page()
        result = asyncio.run(
            douyin_publish_executor._scheduled_submission_readback(
                page,
                title="测试",
                target=target,
                attempts=2,
            )
        )

        self.assertEqual(result["scheduledAt"], "2026-08-09 10:00")
        self.assertEqual(page.scan_count, 2)
        self.assertEqual(page.scroll_count, 1)

    def test_scheduled_readback_allows_platform_card_to_appear_after_three_minutes(self) -> None:
        """抖音卡片延迟超过 90 秒时，不得过早把已提交作品判为待核对。"""

        target = datetime(2026, 8, 9, 16, 30)

        class Page:
            url = "https://creator.douyin.com/creator-micro/content/manage"

            def __init__(self) -> None:
                self.scan_count = 0

            def is_closed(self) -> bool:
                return False

            async def evaluate(self, script: str, payload=None):
                del payload
                if "oneclick-scheduled-card-scan" in script:
                    self.scan_count += 1
                    if self.scan_count >= 181:
                        return [
                            "测试 定时发布中 "
                            "定时: 2026年08月09日 16:30 修改定时"
                        ]
                    return []
                if "oneclick-scheduled-list-scroll" in script:
                    return False
                raise AssertionError(f"未知脚本：{script[:80]}")

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        page = Page()
        result = asyncio.run(
            douyin_publish_executor._scheduled_submission_readback(
                page,
                title="测试",
                target=target,
            )
        )

        self.assertEqual(result["scheduledAt"], "2026-08-09 16:30")
        self.assertEqual(page.scan_count, 181)

    def test_scheduled_readback_refreshes_stale_manage_page_before_timeout(self) -> None:
        """提交跳转后的管理页列表不更新时，刷新后出现的卡片必须能够回读。"""

        target = datetime(2026, 8, 9, 16, 0)

        class Page:
            url = "https://creator.douyin.com/creator-micro/content/manage"

            def __init__(self) -> None:
                self.scan_count = 0
                self.reload_count = 0

            def is_closed(self) -> bool:
                return False

            async def evaluate(self, script: str, payload=None):
                del payload
                if "oneclick-scheduled-card-scan" in script:
                    self.scan_count += 1
                    if self.reload_count:
                        return [
                            "测试 定时发布中 "
                            "定时: 2026年08月09日 16:00 修改定时"
                        ]
                    return []
                if "oneclick-scheduled-list-scroll" in script:
                    return False
                raise AssertionError(f"未知脚本：{script[:80]}")

            async def reload(self, **kwargs) -> None:
                self.assert_reload_options = kwargs
                self.reload_count += 1

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        page = Page()
        result = asyncio.run(
            douyin_publish_executor._scheduled_submission_readback(
                page,
                title="测试",
                target=target,
                attempts=31,
            )
        )

        self.assertEqual(result["scheduledAt"], "2026-08-09 16:00")
        self.assertEqual(page.reload_count, 1)
        self.assertEqual(
            page.assert_reload_options,
            {"wait_until": "domcontentloaded", "timeout": 60_000},
        )

    def test_scheduled_readback_timeout_logs_wait_stage_and_reports_page_state(self) -> None:
        """回执超时时，客户端日志和异常必须说明等待时长与当前页面。"""

        target = datetime(2026, 8, 9, 16, 30)

        class Page:
            url = "https://creator.douyin.com/creator-micro/content/manage"

            def is_closed(self) -> bool:
                return False

            async def evaluate(self, script: str, payload=None):
                del payload
                if "oneclick-scheduled-card-scan" in script:
                    return []
                if "oneclick-scheduled-list-scroll" in script:
                    return False
                raise AssertionError(f"未知脚本：{script[:80]}")

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        messages: list[str] = []
        sink_id = logger.add(lambda message: messages.append(message.record["message"]))
        try:
            with self.assertRaisesRegex(
                douyin_publish_executor.DouyinPublishError,
                "已检查 2 次.*最长等待 2 秒.*作品管理页.*匹配卡片 0 张",
            ):
                asyncio.run(
                    douyin_publish_executor._scheduled_submission_readback(
                        Page(),
                        title="测试",
                        target=target,
                        attempts=2,
                    )
                )
        finally:
            logger.remove(sink_id)

        self.assertTrue(any("开始等待平台回执" in message for message in messages))
        self.assertTrue(any("平台回执等待超时" in message for message in messages))

    def test_scheduled_readback_never_cross_matches_title_and_time_between_cards(self) -> None:
        """标题和时间分别位于两张作品卡时，不得用全页文本拼成成功回执。"""

        target = datetime(2026, 8, 9, 10, 0)

        class Body:
            async def inner_text(self, *, timeout: int) -> str:
                del timeout
                return (
                    "卡片A 测试 定时: 2026年08月09日 11:00 "
                    "卡片B 其他标题 定时: 2026年08月09日 10:00"
                )

        class Page:
            url = "https://creator.douyin.com/creator-micro/content/manage"

            def is_closed(self) -> bool:
                return False

            def locator(self, _selector: str):
                return Body()

            async def evaluate(self, script: str, payload=None):
                del payload
                if "oneclick-scheduled-card-scan" in script:
                    return []
                if "oneclick-scheduled-list-scroll" in script:
                    return False
                raise AssertionError(f"未知脚本：{script[:80]}")

            async def wait_for_timeout(self, milliseconds: int) -> None:
                del milliseconds

            async def reload(self, **_kwargs) -> None:
                return None

        with self.assertRaisesRegex(
            douyin_publish_executor.DouyinPublishError,
            "未能从作品管理页回读",
        ):
            asyncio.run(
                douyin_publish_executor._scheduled_submission_readback(
                    Page(),
                    title="测试",
                    target=target,
                    attempts=1,
                )
            )

    def test_scheduled_card_dom_scan_requires_title_and_time_in_one_visible_card(self) -> None:
        """真实 DOM 扫描不得跨卡片匹配，也不接受隐藏卡片。"""

        async def scenario() -> None:
            from playwright.async_api import async_playwright

            target = datetime(2026, 8, 9, 10, 0)
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.set_content(
                    """
                    <section class="works-list">
                      <article class="work-card">
                        <h3>测试</h3><span>定时发布中</span>
                        <span>定时: 2026年08月09日 11:00</span><button>修改定时</button>
                      </article>
                      <article class="work-card">
                        <h3>其他标题</h3><span>定时发布中</span>
                        <span>定时: 2026年08月09日 10:00</span><button>修改定时</button>
                      </article>
                      <article class="work-card" style="display:none">
                        <h3>测试</h3><span>定时发布中</span>
                        <span>定时: 2026年08月09日 10:00</span><button>修改定时</button>
                      </article>
                    </section>
                    """
                )
                self.assertEqual(
                    await douyin_publish_executor._scheduled_card_texts(
                        page,
                        title="测试",
                        target=target,
                    ),
                    [],
                )
                await page.locator(".works-list").evaluate(
                    """list => list.insertAdjacentHTML('beforeend', `
                    <article class="work-card">
                      <h3>测试</h3><span>定时发布中</span>
                      <span>定时: 2026年08月09日 10:00</span><button>修改定时</button>
                    </article>`);"""
                )
                cards = await douyin_publish_executor._scheduled_card_texts(
                    page,
                    title="测试",
                    target=target,
                )
                self.assertEqual(len(cards), 1)
                self.assertIn("2026年08月09日 10:00", cards[0])
                await browser.close()

        asyncio.run(scenario())

    def test_scheduled_card_dom_scan_reaches_card_title_above_nested_schedule_controls(self) -> None:
        """定时控件行不是作品卡边界，必须继续上溯到同卡标题。"""

        async def scenario() -> None:
            from playwright.async_api import async_playwright

            target = datetime(2026, 8, 9, 16, 0)
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.set_content(
                    """
                    <section class="works-list">
                      <article class="video-card-info">
                        <h3>测试</h3>
                        <div class="info-row">
                          <div class="schedule-controls">
                            <span>定时发布中</span>
                            <span>定时: 2026年08月09日 16:00</span>
                            <button>修改定时</button>
                          </div>
                        </div>
                      </article>
                    </section>
                    """
                )

                cards = await douyin_publish_executor._scheduled_card_texts(
                    page,
                    title="测试",
                    target=target,
                )

                self.assertEqual(len(cards), 1)
                self.assertIn("测试", cards[0])
                self.assertIn("2026年08月09日 16:00", cards[0])
                await browser.close()

        asyncio.run(scenario())

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

    def test_identity_readback_uses_platform_nickname_before_profile_name(self) -> None:
        """主体编号不能替代抖音官方回执中的账号昵称。"""

        self.assertEqual(
            douyin_publish_executor._expected_account_name(
                {"profileName": "3199", "userName": "逆浪风"}
            ),
            "逆浪风",
        )
        self.assertEqual(
            douyin_publish_executor._expected_account_name(
                {"profileName": "旧主体", "userName": ""}
            ),
            "旧主体",
        )

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

    def test_editor_body_retries_when_previous_text_is_appended(self) -> None:
        """Windows 富文本编辑器保留旧文本时，应清空后重试而非直接报同步失败。"""

        video = DouYinVideo(
            title="测试标题",
            file_path="/tmp/demo.mp4",
            tags=[],
            publish_date=datetime.now(),
            account_file="/tmp/account.json",
            description="测试",
        )
        editor = MagicMock()
        editor.fill = AsyncMock()
        editor.click = AsyncMock()
        page = MagicMock()
        page.keyboard.press = AsyncMock()
        page.keyboard.insert_text = AsyncMock()
        page.wait_for_timeout = AsyncMock()

        # 第一次写入后回读为“测试测试”，第二次清空、写入后回读正确。
        with patch.object(
            video,
            "_read_raw_editor_text",
            new_callable=AsyncMock,
            side_effect=["", "测试测试", "", "测试"],
        ) as read_back:
            import asyncio

            asyncio.run(video._fill_editor_body(page, editor, "测试"))

        self.assertEqual(editor.fill.await_count, 2)
        self.assertEqual(read_back.await_count, 4)
        self.assertEqual(page.keyboard.insert_text.await_count, 2)

    def test_editor_body_uses_meta_select_all_on_macos(self) -> None:
        video = DouYinVideo(
            title="测试标题",
            file_path="/tmp/demo.mp4",
            tags=[],
            publish_date=datetime.now(),
            account_file="/tmp/account.json",
            description="测试",
        )
        editor = MagicMock()
        editor.fill = AsyncMock()
        editor.click = AsyncMock()
        page = MagicMock()
        page.keyboard.press = AsyncMock()
        page.keyboard.insert_text = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        with patch("uploader.douyin_uploader.main.sys.platform", "darwin"), patch.object(
            video,
            "_read_raw_editor_text",
            new_callable=AsyncMock,
            side_effect=["", "测试"],
        ):
            asyncio.run(video._fill_editor_body(page, editor, "测试"))

        self.assertEqual(page.keyboard.press.await_args_list[0].args, ("Meta+A",))

    def test_title_is_cleared_and_read_back_before_new_value_is_written(self) -> None:
        """独立标题必须先确认旧值已清空，再写入新标题并二次回读。"""

        video = DouYinVideo(
            title="新标题",
            file_path="/tmp/demo.mp4",
            tags=[],
            publish_date=datetime.now(),
            account_file="/tmp/account.json",
            description="测试",
        )
        title_input = MagicMock()
        title_input.fill = AsyncMock()
        title_input.input_value = AsyncMock(side_effect=["", "新标题"])
        with patch.object(
            video,
            "_visible_title_inputs",
            new_callable=AsyncMock,
            return_value=[title_input],
        ):
            asyncio.run(video.clear_platform_title(MagicMock()))

        self.assertEqual(title_input.fill.await_args_list[0].args, ("",))
        self.assertEqual(title_input.fill.await_args_list[1].args, ("新标题",))
        self.assertEqual(title_input.input_value.await_count, 2)

    def test_title_waits_for_editor_fields_after_upload_redirect(self) -> None:
        video = DouYinVideo(
            title="测试标题",
            file_path=str(self.video),
            tags=[],
            publish_date=0,
            account_file="offline.json",
            description="测试正文",
        )
        title_input = MagicMock()
        title_input.fill = AsyncMock()
        title_input.input_value = AsyncMock(side_effect=["", "测试标题"])
        page = MagicMock()
        page.wait_for_timeout = AsyncMock()

        with patch.object(
            video,
            "_visible_title_inputs",
            new_callable=AsyncMock,
            side_effect=[[], [], [title_input]],
        ) as visible_inputs:
            asyncio.run(video.clear_platform_title(page))

        self.assertEqual(visible_inputs.await_count, 3)
        self.assertEqual(page.wait_for_timeout.await_count, 2)
        self.assertEqual(title_input.fill.await_count, 2)

    def test_headless_sms_challenge_waits_for_native_code_without_revealing_page(self) -> None:
        """验证码应只在同一无头页面填写，不能转为前台浏览器。"""

        class Controls:
            def __init__(self, items) -> None:
                self.items = list(items)

            async def count(self) -> int:
                return len(self.items)

            def nth(self, index: int):
                return self.items[index]

        class Textbox:
            def __init__(self, name: str) -> None:
                self.name = name
                self.value = ""

            async def is_visible(self) -> bool:
                return True

            async def is_enabled(self) -> bool:
                return True

            async def fill(self, value: str) -> None:
                self.value = value

            async def input_value(self) -> str:
                return self.value

        class ConfirmButton:
            def __init__(self, page) -> None:
                self.page = page

            async def is_visible(self) -> bool:
                return True

            async def is_enabled(self) -> bool:
                # 真实抖音“验证”按钮会在验证码写入前禁用；回归用例要求
                # 执行器先定位可见按钮，填写并回读后才等待它启用。
                return bool(self.page.textbox.value)

            async def click(self, *, timeout: int) -> None:
                self.page.url = "https://creator.douyin.com/creator-micro/content/manage"

        class Marker:
            def __init__(self, container) -> None:
                self.container = container

            async def is_visible(self) -> bool:
                return True

            def locator(self, _selector: str):
                return Controls([self.container])

        class ImageControl:
            async def is_visible(self) -> bool:
                return True

            async def is_enabled(self) -> bool:
                return True

        class PublishButton:
            def __init__(self) -> None:
                self.click_count = 0

            async def is_visible(self) -> bool:
                return True

            async def is_enabled(self) -> bool:
                return True

            async def click(self, *, timeout: int) -> None:
                del timeout
                self.click_count += 1

        class VerificationContainer:
            def __init__(self, page) -> None:
                self.page = page

            async def is_visible(self) -> bool:
                return True

            async def evaluate(self, _script: str) -> str:
                return "sms-verification-container"

            def get_by_role(self, role: str, **_kwargs):
                if role == "textbox":
                    return Controls([self.page.textbox])
                if role == "button":
                    return Controls([self.page.confirm])
                return Controls([])

        class SmsChallengePage:
            def __init__(self) -> None:
                self.url = "https://creator.douyin.com/verification"
                self.title = Textbox("底层标题")
                self.body = Textbox("底层正文")
                self.textbox = Textbox("验证短信码")
                self.confirm = ConfirmButton(self)
                self.publish = PublishButton()
                self.avatar = ImageControl()
                self.video_preview = ImageControl()
                self.container = VerificationContainer(self)
                self.marker = Marker(self.container)

            def get_by_text(self, text: str, *, exact: bool):
                if text == "接收短信验证码" and exact:
                    return Controls([self.marker])
                return Controls([])

            def get_by_role(self, role: str, **_kwargs):
                if role == "textbox":
                    return Controls([self.title, self.body, self.textbox])
                if role == "button":
                    return Controls([self.publish, self.confirm])
                if role == "img":
                    return Controls([self.avatar, self.video_preview])
                return Controls([])

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        page = SmsChallengePage()
        video = DouYinVideo(
            title="测试标题",
            file_path="/tmp/demo.mp4",
            tags=[],
            publish_date=datetime.now(),
            account_file="/tmp/account.json",
            description="测试文案",
        )

        async def submit_native_sms_code(challenge) -> None:
            await video.apply_sms_verification_code(page, challenge, "123456")

        with patch("utils.base_social_media.reveal_page_window") as reveal:
            receipt = asyncio.run(
                video._wait_formal_publish_result(
                    page,
                    on_verification=submit_native_sms_code,
                )
            )

        self.assertEqual(receipt["status"], "published")
        reveal.assert_not_called()
        self.assertEqual(page.textbox.value, "123456")
        self.assertEqual(page.title.value, "")
        self.assertEqual(page.body.value, "")
        self.assertEqual(page.publish.click_count, 0)

    def test_multiple_visible_sms_inputs_stop_without_filling_any_value(self) -> None:
        """多个可见验证码输入框不能猜测目标控件。"""

        class Controls:
            def __init__(self, items) -> None:
                self.items = list(items)

            async def count(self) -> int:
                return len(self.items)

            def nth(self, index: int):
                return self.items[index]

        class Textbox:
            def __init__(self) -> None:
                self.value = ""

            async def is_visible(self) -> bool:
                return True

            async def is_enabled(self) -> bool:
                return True

            async def fill(self, value: str) -> None:
                self.value = value

        class VerificationContainer:
            def __init__(self, inputs) -> None:
                self.inputs = inputs

            async def is_visible(self) -> bool:
                return True

            async def evaluate(self, _script: str) -> str:
                return "ambiguous-input-container"

            def get_by_role(self, role: str, **_kwargs):
                if role == "textbox":
                    return Controls(self.inputs)
                return Controls([])

        class Marker:
            def __init__(self, container) -> None:
                self.container = container

            async def is_visible(self) -> bool:
                return True

            def locator(self, _selector: str):
                return Controls([self.container])

        class AmbiguousPage:
            url = "https://creator.douyin.com/verification"

            def __init__(self) -> None:
                self.inputs = [Textbox(), Textbox()]
                self.container = VerificationContainer(self.inputs)
                self.marker = Marker(self.container)

            def get_by_text(self, text: str, *, exact: bool):
                if text == "接收短信验证码" and exact:
                    return Controls([self.marker])
                return Controls([])

            def get_by_role(self, role: str, **_kwargs):
                if role == "textbox":
                    return Controls(self.inputs)
                return Controls([])

        video = DouYinVideo(
            title="测试标题",
            file_path="/tmp/demo.mp4",
            tags=[],
            publish_date=datetime.now(),
            account_file="/tmp/account.json",
            description="测试文案",
        )
        page = AmbiguousPage()
        with self.assertRaisesRegex(RuntimeError, "无法唯一确认"):
            asyncio.run(video.detect_publish_verification(page))
        self.assertEqual([item.value for item in page.inputs], ["", ""])

    def test_multiple_verification_containers_stop_before_reading_controls(self) -> None:
        """两个可见验证弹层时，不能任选其一读取短信或二维码控件。"""

        class Controls:
            def __init__(self, items) -> None:
                self.items = list(items)

            async def count(self) -> int:
                return len(self.items)

            def nth(self, index: int):
                return self.items[index]

        class Container:
            def __init__(self, identity: str) -> None:
                self.identity = identity

            async def is_visible(self) -> bool:
                return True

            async def evaluate(self, _script: str) -> str:
                return self.identity

            def get_by_role(self, _role: str, **_kwargs):
                raise AssertionError("容器不唯一时不应读取内部控件")

        class Marker:
            def __init__(self, container) -> None:
                self.container = container

            async def is_visible(self) -> bool:
                return True

            def locator(self, _selector: str):
                return Controls([self.container])

        class Page:
            url = "https://creator.douyin.com/verification"

            def __init__(self) -> None:
                self.markers = [Marker(Container("dialog-a")), Marker(Container("dialog-b"))]

            def get_by_text(self, text: str, *, exact: bool):
                if text == "接收短信验证码" and exact:
                    return Controls(self.markers)
                return Controls([])

            def get_by_role(self, _role: str, **_kwargs):
                raise AssertionError("容器不唯一时不应读取全页控件")

        video = DouYinVideo(
            title="测试标题",
            file_path="/tmp/demo.mp4",
            tags=[],
            publish_date=datetime.now(),
            account_file="/tmp/account.json",
            description="测试文案",
        )
        with self.assertRaisesRegex(RuntimeError, "验证容器无法唯一确认"):
            asyncio.run(video.detect_publish_verification(Page()))

    def test_unanchored_visible_verification_marker_stops_before_reading_controls(self) -> None:
        """任一可见验证文案无法反查弹层时，不能忽略后继续使用另一弹层。"""

        class Controls:
            def __init__(self, items) -> None:
                self.items = list(items)

            async def count(self) -> int:
                return len(self.items)

            def nth(self, index: int):
                return self.items[index]

        class Container:
            async def is_visible(self) -> bool:
                return True

            async def evaluate(self, _script: str) -> str:
                return "only-valid-dialog"

            def get_by_role(self, _role: str, **_kwargs):
                raise AssertionError("验证文案存在未映射容器时不应读取任何控件")

        class Marker:
            def __init__(self, candidates) -> None:
                self.candidates = candidates

            async def is_visible(self) -> bool:
                return True

            def locator(self, _selector: str):
                return Controls(self.candidates)

        class Page:
            url = "https://creator.douyin.com/verification"

            def __init__(self) -> None:
                self.markers = [Marker([Container()]), Marker([])]

            def get_by_text(self, text: str, *, exact: bool):
                if text == "接收短信验证码" and exact:
                    return Controls(self.markers)
                return Controls([])

        video = DouYinVideo(
            title="测试标题",
            file_path="/tmp/demo.mp4",
            tags=[],
            publish_date=datetime.now(),
            account_file="/tmp/account.json",
            description="测试文案",
        )
        with self.assertRaisesRegex(RuntimeError, "验证容器无法唯一确认"):
            asyncio.run(video.detect_publish_verification(Page()))

    def test_hidden_verification_container_stops_before_reading_controls(self) -> None:
        """验证文案祖先不可见时，不能读取其中的短信、确认或二维码控件。"""

        class Controls:
            def __init__(self, items) -> None:
                self.items = list(items)

            async def count(self) -> int:
                return len(self.items)

            def nth(self, index: int):
                return self.items[index]

        class HiddenContainer:
            def __init__(self) -> None:
                self.queried_roles: list[str] = []

            async def is_visible(self) -> bool:
                return False

            def get_by_role(self, role: str, **_kwargs):
                self.queried_roles.append(role)
                return Controls([])

        class Marker:
            def __init__(self, container) -> None:
                self.container = container

            async def is_visible(self) -> bool:
                return True

            def locator(self, _selector: str):
                return Controls([self.container])

        class Page:
            url = "https://creator.douyin.com/verification"

            def __init__(self) -> None:
                self.container = HiddenContainer()
                self.marker = Marker(self.container)

            def get_by_text(self, text: str, *, exact: bool):
                if text == "接收短信验证码" and exact:
                    return Controls([self.marker])
                return Controls([])

        page = Page()
        video = DouYinVideo(
            title="测试标题",
            file_path="/tmp/demo.mp4",
            tags=[],
            publish_date=datetime.now(),
            account_file="/tmp/account.json",
            description="测试文案",
        )
        with self.assertRaisesRegex(RuntimeError, "验证容器无法唯一确认"):
            asyncio.run(video.detect_publish_verification(page))
        self.assertEqual(page.container.queried_roles, [])

    def test_qr_challenge_requires_a_real_decoder_result(self) -> None:
        """有效二维码可经解码器确认，普通高对比方图绝不能仅凭形状通过。"""

        class Controls:
            def __init__(self, items) -> None:
                self.items = list(items)

            async def count(self) -> int:
                return len(self.items)

            def nth(self, index: int):
                return self.items[index]

        class BackgroundTextbox:
            async def is_visible(self) -> bool:
                return True

            async def is_enabled(self) -> bool:
                return True

        class BackgroundButton:
            async def is_visible(self) -> bool:
                return True

            async def is_enabled(self) -> bool:
                return True

        class VerificationContainer:
            def __init__(self, image) -> None:
                self.image = image

            async def is_visible(self) -> bool:
                return True

            async def evaluate(self, _script: str) -> str:
                return "qr-verification-container"

            async def inner_text(self, *, timeout: int) -> str:
                del timeout
                # 仅扫码主态会暴露该文案；短信页的“使用原设备扫码”备用入口
                # 不应被当成二维码状态。
                return "使用原设备扫码"

            def get_by_role(self, role: str, **_kwargs):
                if role == "img":
                    return Controls([self.image])
                return Controls([])

        class Marker:
            def __init__(self, container) -> None:
                self.container = container

            async def is_visible(self) -> bool:
                return True

            def locator(self, _selector: str):
                return Controls([self.container])

        class ImageControl:
            def __init__(self, payload: bytes) -> None:
                self.payload = payload

            async def is_visible(self) -> bool:
                return True

            async def is_enabled(self) -> bool:
                return True

            async def screenshot(self) -> bytes:
                return self.payload

        class QrPage:
            url = "https://creator.douyin.com/verification"

            def __init__(self, payload: bytes) -> None:
                self.image = ImageControl(payload)
                self.container = VerificationContainer(self.image)
                self.marker = Marker(self.container)
                self.title = BackgroundTextbox()
                self.body = BackgroundTextbox()
                self.publish = BackgroundButton()
                self.avatar = ImageControl(payload)
                self.video_preview = ImageControl(payload)

            def get_by_text(self, text: str, *, exact: bool):
                if text == "使用原设备扫码" and exact:
                    return Controls([self.marker])
                return Controls([])

            def get_by_role(self, role: str, **_kwargs):
                if role == "textbox":
                    return Controls([self.title, self.body])
                if role == "button":
                    return Controls([self.publish])
                if role == "img":
                    return Controls([self.avatar, self.video_preview, self.image])
                return Controls([])

        qr = qrcode.make("offline-verification")
        qr_output = BytesIO()
        qr.save(qr_output, format="PNG")
        checker = Image.new("1", (240, 240), "white")
        for left in range(0, 240, 12):
            for top in range(0, 240, 12):
                if (left // 12 + top // 12) % 2:
                    for x in range(left, left + 12):
                        for y in range(top, top + 12):
                            checker.putpixel((x, y), 0)
        checker_output = BytesIO()
        checker.save(checker_output, format="PNG")

        video = DouYinVideo(
            title="测试标题",
            file_path="/tmp/demo.mp4",
            tags=[],
            publish_date=datetime.now(),
            account_file="/tmp/account.json",
            description="测试文案",
        )
        challenge = asyncio.run(video.detect_publish_verification(QrPage(qr_output.getvalue())))
        self.assertEqual(challenge.kind, "qr")
        with self.assertRaisesRegex(RuntimeError, "二维码"):
            asyncio.run(video.detect_publish_verification(QrPage(checker_output.getvalue())))

    def test_submitted_sms_panel_never_restarts_sms_or_parses_ordinary_image(self) -> None:
        """短信已提交的过渡态不能重发验证码，也不能误把普通图片视为二维码。"""

        class Controls:
            def __init__(self, count: int) -> None:
                self._count = count

            async def count(self) -> int:
                return self._count

        class Panel:
            def get_by_text(self, text: str, *, exact: bool):
                if text != "接收短信验证码" or not exact:
                    raise AssertionError("只应读取短信主态标记")
                return Controls(1)

            async def inner_text(self, *, timeout: int) -> str:
                del timeout
                return "接收短信验证码 使用原设备扫码"

            def get_by_role(self, _role: str, **_kwargs):
                raise AssertionError("短信已提交的过渡态不应读取图片或其他控件")

        class Page:
            url = "https://creator.douyin.com/verification"

        video = DouYinVideo(
            title="测试标题",
            file_path="/tmp/demo.mp4",
            tags=[],
            publish_date=datetime.now(),
            account_file="/tmp/account.json",
            description="测试文案",
        )
        video._sms_verification_submitted = True
        panel = Panel()
        with patch.object(video, "_unique_publish_verification_container", new_callable=AsyncMock, return_value=panel):
            challenge = asyncio.run(video.detect_publish_verification(Page()))
        self.assertIsNone(challenge)

    def test_unknown_verification_page_never_becomes_qr_success(self) -> None:
        """仍在验证页却缺少可识别控件时，必须停止而不是返回 None。"""

        class Controls:
            async def count(self) -> int:
                return 0

            def nth(self, _index: int):
                raise AssertionError("不应读取不存在控件")

        class Page:
            url = "https://creator.douyin.com/verification"

            def get_by_text(self, _text: str, *, exact: bool):
                return Controls()

        video = DouYinVideo(
            title="测试标题",
            file_path="/tmp/demo.mp4",
            tags=[],
            publish_date=datetime.now(),
            account_file="/tmp/account.json",
            description="测试文案",
        )
        with self.assertRaisesRegex(RuntimeError, "验证页面状态无法识别"):
            asyncio.run(video.detect_publish_verification(Page()))


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

    def test_single_video_executor_rejects_batch_workflow(self) -> None:
        batch_payload = {
            **self.payload,
            "workflow": "douyin-commerce-batch",
            "batchWorkflow": "douyin-commerce-batch",
        }

        with self.assertRaisesRegex(
            douyin_publish_executor.DouyinPublishError, "批量执行器"
        ):
            douyin_publish_executor.validate_douyin_publish_payload(batch_payload)


if __name__ == "__main__":
    unittest.main()
