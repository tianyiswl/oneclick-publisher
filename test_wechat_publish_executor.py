# -*- coding: utf-8 -*-
"""公众号正式发布主链路的离线边界测试。"""

import base64
from io import BytesIO
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

import qrcode

from app_core.publish_service import _validate_payloads
from app_core.douyin_publish_executor import DouyinPublishError
from app_core.wechat_publish_executor import (
    WechatPublishError,
    _capture_qr_image,
    _close_publish_session,
    _immediate_home_readback,
    _is_transient_navigation_error,
    _page_state_after_navigation,
    _platform_date_label,
    _recover_post_submit_result,
    _resolve_verified_submit_result,
    _safe_dialog_report,
    _scheduled_home_readback,
)
from app_core.wechat_verification import validate_qr_image_bytes


class WechatPublishExecutorBoundaryTests(unittest.TestCase):
    def test_err_aborted_after_submit_is_a_transient_navigation_error(self):
        """公众号已接收提交后的首页跳转中断不能被写成发表失败。"""

        error = RuntimeError(
            "Page.goto: net::ERR_ABORTED at https://mp.weixin.qq.com/"
        )

        self.assertTrue(_is_transient_navigation_error(error))

    def _payload(self, root: Path) -> dict:
        cover = root / "cover.png"
        cover.write_bytes(b"png")
        return {
            "type": 10,
            "contentType": "text",
            "title": "定时发表测试",
            "description": "正文",
            "fileList": [],
            "coverPath": str(cover),
            "accountList": ["oneclick_10_test.json"],
            "runtimeMode": "publish",
            "debugDryRun": False,
            "wechatGroupNotification": True,
            "enableTimer": True,
            "scheduleTime": "2099-08-01 09:00",
            "scheduleTimezone": "Asia/Shanghai",
            "originalDeclaration": False,
        }

    def test_formal_publish_rejects_douyin_non_video_content(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = self._payload(Path(directory))
            self.assertEqual(_validate_payloads([payload])[0]["type"], 10)
            payload["type"] = 3
        with self.assertRaisesRegex(DouyinPublishError, "只接入视频类型"):
            _validate_payloads([payload])

    def test_formal_publish_requires_explicit_non_dry_run(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = self._payload(Path(directory))
            payload["debugDryRun"] = True
            with self.assertRaisesRegex(ValueError, "debugDryRun=false"):
                _validate_payloads([payload])

    def test_schedule_must_be_enabled_and_future(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = self._payload(Path(directory))
            payload["enableTimer"] = False
            with self.assertRaisesRegex(ValueError, "不得携带"):
                _validate_payloads([payload])

    def test_preflight_still_requires_dry_run(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = self._payload(Path(directory))
            payload["runtimeMode"] = "preflight"
            payload["debugDryRun"] = True
            self.assertEqual(_validate_payloads([payload])[0]["runtimeMode"], "preflight")

    def test_platform_date_label_maps_today_tomorrow_and_later(self):
        today = date(2026, 7, 31)
        self.assertEqual(_platform_date_label(today, today=today), "今天")
        self.assertEqual(
            _platform_date_label(date(2026, 8, 1), today=today),
            "明天",
        )
        self.assertEqual(
            _platform_date_label(date(2026, 8, 2), today=today),
            "8月2日",
        )

    def test_unknown_dialog_report_keeps_visible_copy_and_redacts_token(self):
        report = _safe_dialog_report(
            {
                "dialogs": [
                    {
                        "title": "定时发表确认",
                        "body": "内容将在明天 09:00 发表 token=secret",
                        "buttons": ["继续", "取消"],
                    }
                ],
                "qrCount": 0,
                "qrText": [],
            }
        )
        self.assertEqual(report["kind"], "dialog")
        self.assertEqual(
            report["dialogs"][0]["buttons"],
            ["继续", "取消"],
        )
        self.assertNotIn("secret", str(report))

    def test_qr_report_never_records_dialog_or_qr_contents(self):
        report = _safe_dialog_report(
            {
                "qrCount": 1,
                "qrText": ["二维码"],
                "dialogs": [
                    {
                        "title": "扫码",
                        "body": "二维码原始内容 qr-secret",
                        "buttons": [],
                    }
                ],
            }
        )
        self.assertEqual(report["kind"], "qr")
        self.assertNotIn("qr-secret", str(report))
        self.assertNotIn("dialogs", report)

    def test_scheduled_home_card_is_independent_success_readback(self):
        target = datetime.now().replace(
            hour=9,
            minute=0,
            second=0,
            microsecond=0,
        ) + timedelta(days=1)
        decision = _scheduled_home_readback(
            {
                "scheduledCards": [
                    (
                        "定时发表 明天 09:00 已开启群发通知 "
                        "AI 让你多会一项工作后，先别急着接下那个结果"
                    )
                ]
            },
            {
                "scheduledPublish": True,
                "scheduleLocal": target.strftime("%Y-%m-%d %H:%M"),
                "groupNotification": True,
            },
        )
        self.assertTrue(decision["ok"])
        self.assertEqual(decision["time"], "09:00")

    def test_scheduled_home_card_rejects_time_or_group_mismatch(self):
        state = {
            "scheduledCards": [
                "定时发表 明天 09:01 AI 让你多会一项工作后"
            ]
        }
        preferences = {
            "scheduledPublish": True,
            "scheduleLocal": "2026-08-01 09:00",
            "groupNotification": True,
        }
        self.assertFalse(_scheduled_home_readback(state, preferences)["ok"])

    def test_immediate_home_card_confirms_title_and_unique_article_link(self):
        title = "具身 AI 从会动走向能交接任务"
        result = _immediate_home_readback(
            {
                "publishedCards": [
                    f"今天 15:05 已发表 {title} 0 0 0 0",
                ],
                "links": [
                    {
                        "text": title,
                        "href": "https://mp.weixin.qq.com/s/example",
                    }
                ],
            },
            title,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["publishedAt"], "15:05")
        self.assertEqual(result["link"], "https://mp.weixin.qq.com/s/example")

    def test_immediate_home_card_rejects_draft_or_ambiguous_link(self):
        title = "即刻发表回读测试"
        draft = _immediate_home_readback(
            {
                "publishedCards": [f"今天 15:05 草稿 {title}"],
                "links": [
                    {
                        "text": title,
                        "href": "https://mp.weixin.qq.com/s/example",
                    }
                ],
            },
            title,
        )
        self.assertFalse(draft["ok"])
        ambiguous = _immediate_home_readback(
            {
                "publishedCards": [f"今天 15:05 已发表 {title}"],
                "links": [
                    {"text": title, "href": "https://mp.weixin.qq.com/s/first"},
                    {"text": title, "href": "https://mp.weixin.qq.com/s/second"},
                ],
            },
            title,
        )
        self.assertFalse(ambiguous["ok"])

    def test_navigation_context_error_is_recoverable_only_when_exactly_matched(self):
        self.assertTrue(
            _is_transient_navigation_error(
                RuntimeError("Page.evaluate: Execution context was destroyed")
            )
        )
        self.assertFalse(
            _is_transient_navigation_error(
                RuntimeError("平台出现未知合规确认")
            )
        )


class WechatQrCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.page = await self.browser.new_page(viewport={"width": 900, "height": 900})

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    @staticmethod
    def _data_url(text: str) -> str:
        image = qrcode.make(text)
        output = BytesIO()
        image.save(output, format="PNG")
        return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()

    async def test_capture_skips_blank_prompt_container_and_uses_qr_pixels(self):
        data_url = self._data_url("oneclick-capture-test")
        await self.page.set_content(
            f"""
            <div data-oneclick-wechat-qr="0"
                 style="width:520px;height:520px;background:#fff;text-align:center">
              <span>扫码后，请联系管理员进行验证</span>
              <img data-oneclick-wechat-qr="1"
                   src="{data_url}" width="290" height="290"
                   style="display:block;margin:20px auto" />
            </div>
            """
        )
        image = await _capture_qr_image(self.page)
        self.assertEqual(validate_qr_image_bytes(image), image)

    async def test_capture_rejects_blank_prompt_without_qr(self):
        await self.page.set_content(
            """
            <div data-oneclick-wechat-qr="0"
                 style="width:520px;height:520px;background:#fff;text-align:center">
              <span>扫码后，请联系管理员进行验证</span>
            </div>
            """
        )
        with self.assertRaises(WechatPublishError):
            await _capture_qr_image(self.page)

    async def test_page_state_retries_destroyed_navigation_context(self):
        class NavigatingPage:
            def __init__(self):
                self.calls = 0
                self.waited = False
                self.page_timeout_calls = 0

            async def evaluate(self, _script, _title):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError(
                        "Execution context was destroyed, most likely because of a navigation"
                    )
                return {"successMarkers": ["定时发表成功"]}

            async def wait_for_load_state(self, _state, timeout):
                self.waited = timeout == 2_000

            async def wait_for_timeout(self, _milliseconds):
                self.page_timeout_calls += 1
                raise AssertionError("导航重试不应依赖页面执行上下文计时")

        page = NavigatingPage()
        state = await _page_state_after_navigation(page, "", timeout_seconds=3)
        self.assertIn("定时发表成功", state["successMarkers"])
        self.assertTrue(page.waited)
        self.assertEqual(page.page_timeout_calls, 0)

    async def test_post_submit_navigation_recovers_exact_immediate_home_readback(self):
        title = "扫码后跳转回读测试"

        class HomePage:
            def __init__(self, case):
                self.case = case
                self.goto_calls = 0

            async def goto(self, _url, *, wait_until, timeout):
                self.goto_calls += 1
                self.case.assertEqual(wait_until, "domcontentloaded")
                self.case.assertEqual(timeout, 45_000)

            async def evaluate(self, _script, expected_title):
                self.case.assertEqual(expected_title, title)
                return {
                    "publishedCards": [f"今天 15:05 已发表 {title}"],
                    "links": [
                        {
                            "text": title,
                            "href": "https://mp.weixin.qq.com/s/example",
                        }
                    ],
                    "successMarkers": [],
                }

        page = HomePage(self)
        result = await _recover_post_submit_result(
            page,
            title=title,
            preferences={"scheduledPublish": False, "scheduleLocal": ""},
            ai_accepted=False,
            preflight_message="本地预检完成",
            execution_record={},
            attempts=1,
            settle_seconds=0,
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["actuallyPublished"])
        self.assertTrue(result["recoveredAfterNavigation"])
        self.assertEqual(page.goto_calls, 1)

    async def test_post_submit_navigation_recovers_exact_scheduled_home_card(self):
        target = datetime.now() + timedelta(days=1)
        target = target.replace(hour=9, minute=0, second=0, microsecond=0)
        title = "扫码后定时回读测试"
        preferences = {
            "scheduledPublish": True,
            "scheduleLocal": target.strftime("%Y-%m-%d %H:%M"),
            "groupNotification": True,
        }
        date_label = _platform_date_label(target.date())

        class HomePage:
            async def goto(self, _url, *, wait_until, timeout):
                return None

            async def evaluate(self, _script, expected_title):
                return {
                    "scheduledCards": [
                        f"定时发表 {date_label} 09:00 已开启群发通知 {expected_title}"
                    ],
                    "links": [],
                    "successMarkers": [],
                }

        result = await _recover_post_submit_result(
            HomePage(),
            title=title,
            preferences=preferences,
            ai_accepted=False,
            preflight_message="本地预检完成",
            execution_record={},
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["scheduled"])
        self.assertFalse(result["actuallyPublished"])
        self.assertEqual(result["scheduledAt"], preferences["scheduleLocal"])

    async def test_verified_submit_immediately_resolves_exact_home_readback(self):
        title = "验证成功后立即回读"

        class HomePage:
            def __init__(self):
                self.goto_calls = 0

            async def goto(self, _url, *, wait_until, timeout):
                self.goto_calls += 1

            async def evaluate(self, _script, expected_title):
                return {
                    "publishedCards": [f"今天 17:43 已发表 {expected_title}"],
                    "links": [
                        {
                            "text": expected_title,
                            "href": "https://mp.weixin.qq.com/s/verified",
                        }
                    ],
                    "successMarkers": [],
                }

        page = HomePage()
        result = await _resolve_verified_submit_result(
            page,
            title=title,
            preferences={"scheduledPublish": False, "scheduleLocal": ""},
            ai_accepted=False,
            preflight_message="本地预检完成",
            execution_record={},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["links"][0]["href"], "https://mp.weixin.qq.com/s/verified")
        self.assertEqual(page.goto_calls, 1)

    async def test_verified_submit_without_exact_readback_stops_republish(self):
        class EmptyHomePage:
            async def goto(self, _url, *, wait_until, timeout):
                return None

            async def evaluate(self, _script, expected_title):
                return {
                    "publishedCards": [],
                    "links": [],
                    "successMarkers": [],
                }

        with self.assertRaises(WechatPublishError) as raised:
            await _resolve_verified_submit_result(
                EmptyHomePage(),
                title="结果不明测试",
                preferences={"scheduledPublish": False, "scheduleLocal": ""},
                ai_accepted=False,
                preflight_message="本地预检完成",
                execution_record={},
                attempts=1,
                settle_seconds=0,
            )

        self.assertEqual(raised.exception.error_code, "wechat_publish_result_unknown")
        self.assertIn("禁止自动重发", str(raised.exception))

    async def test_cleanup_failure_cannot_override_a_verified_publish_result(self):
        class BrokenCloser:
            def __init__(self):
                self.calls = 0

            async def close(self):
                self.calls += 1
                raise RuntimeError("Connection closed while reading from the driver")

        context = BrokenCloser()
        browser = BrokenCloser()
        await _close_publish_session(context, browser)
        self.assertEqual(context.calls, 1)
        self.assertEqual(browser.calls, 1)


if __name__ == "__main__":
    unittest.main()
