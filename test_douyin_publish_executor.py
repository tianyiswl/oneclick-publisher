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

import qrcode
from PIL import Image

from app_core import douyin_publish_executor
from uploader.douyin_uploader.main import DouYinVideo

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
                return True

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
