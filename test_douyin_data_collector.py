# -*- coding: utf-8 -*-
"""抖音登录会话只读数据采集器测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_core.douyin_data_collector import (
    DOUYIN_DASHBOARD_URL,
    DouyinDataCollectionError,
    DouyinDataCollector,
)
from app_core.platform_data_collectors import collector_for_platform
from app_core.platform_data_models import CollectionFailure


class FakeResponse:
    def __init__(self, payload: object, *, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


class FakeCookieJar:
    def __init__(self) -> None:
        self.entries: list[tuple[str, str, str, str]] = []

    def set(self, name: str, value: str, *, domain: str, path: str) -> None:
        self.entries.append((name, value, domain, path))


class FakeSession:
    def __init__(
        self,
        payload: object,
        *,
        status_code: int = 200,
        error: BaseException | None = None,
    ) -> None:
        self.cookies = FakeCookieJar()
        self.headers: dict[str, str] = {}
        self.payload = payload
        self.status_code = status_code
        self.error = error
        self.calls: list[tuple[str, dict, float]] = []
        self.closed = 0

    def post(self, url: str, *, json: dict, timeout: float) -> FakeResponse:
        self.calls.append((url, dict(json), timeout))
        if self.error is not None:
            raise self.error
        return FakeResponse(self.payload, status_code=self.status_code)

    def close(self) -> None:
        self.closed += 1


class DouyinDirectCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.cookie_dir = Path(self.tempdir.name)
        self.state_file = self.cookie_dir / "oneclick_3_test.json"
        self.state_file.write_text(
            json.dumps(
                {
                    "cookies": [
                        {
                            "name": "sessionid",
                            "value": "private-session",
                            "domain": ".creator.douyin.com",
                            "path": "/",
                        },
                        {
                            "name": "shared",
                            "value": "allowed",
                            "domain": ".douyin.com",
                            "path": "/",
                        },
                        {
                            "name": "foreign",
                            "value": "must-not-leak",
                            "domain": ".example.com",
                            "path": "/",
                        },
                    ],
                    "origins": [],
                }
            ),
            encoding="utf-8",
        )
        self.account = {
            "id": 12,
            "type": 3,
            "filePath": self.state_file.name,
            "userName": "不进入公开载荷",
        }
        self.cookie_patch = patch(
            "app_core.douyin_data_collector.COOKIE_DIR",
            self.cookie_dir,
        )
        self.cookie_patch.start()

    def tearDown(self) -> None:
        self.cookie_patch.stop()
        self.tempdir.cleanup()

    @staticmethod
    def _valid_payload() -> dict:
        return {
            "status_code": 0,
            "status_msg": "",
            "metrics": [
                {
                    "english_metric_name": "play_cnt",
                    "trends": [{"date_time": "20260820", "value": 125}],
                },
                {
                    "english_metric_name": "digg_cnt",
                    "trends": [{"date_time": "20260820", "value": 9}],
                },
                {
                    "english_metric_name": "private_cookie_value",
                    "trends": [{"date_time": "20260820", "value": 999}],
                },
            ],
        }

    def test_valid_direct_payload_is_normalized_and_cookie_domain_is_isolated(self) -> None:
        """放宽 Cookie 域或指标白名单会泄露外域会话或未知字段。"""

        session = FakeSession(self._valid_payload())
        collector = DouyinDataCollector(session_factory=lambda: session)

        batch = collector.collect_direct(self.account)

        self.assertEqual(batch.platform_type, 3)
        self.assertEqual(batch.source_mode, "direct_session")
        self.assertEqual(
            [(item.metric_key, item.metric_value) for item in batch.metrics],
            [("views", 125), ("likes", 9)],
        )
        self.assertEqual(
            [(name, domain) for name, _value, domain, _path in session.cookies.entries],
            [
                ("sessionid", ".creator.douyin.com"),
                ("shared", ".douyin.com"),
            ],
        )
        self.assertEqual(
            session.calls,
            [(DOUYIN_DASHBOARD_URL, {"recent_days": 30}, 20.0)],
        )
        self.assertEqual(session.closed, 1)

    def test_http_200_with_nested_login_rejection_is_not_success(self) -> None:
        """真实探测中的内层 status_code=8 必须进入登录失败而非空指标。"""

        session = FakeSession(
            {
                "status_code": 0,
                "data": {
                    "play": {
                        "status_code": 8,
                        "status_message": "用户未登录",
                    }
                },
            }
        )
        collector = DouyinDataCollector(session_factory=lambda: session)

        with self.assertRaises(DouyinDataCollectionError) as raised:
            collector.collect_direct(self.account)

        self.assertEqual(raised.exception.error_code, "login_required")
        self.assertTrue(raised.exception.fallback_allowed)
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(session.closed, 1)

    def test_empty_success_envelope_is_not_a_zero_metric_sync(self) -> None:
        """空 metrics 若被当成功，UI 会把无数据误解为业务零值。"""

        collector = DouyinDataCollector(
            session_factory=lambda: FakeSession(
                {"status_code": 0, "status_msg": "", "metrics": [{}]}
            )
        )

        with self.assertRaises(DouyinDataCollectionError) as raised:
            collector.collect_direct(self.account)

        self.assertEqual(raised.exception.error_code, "metric_payload_empty")
        self.assertTrue(raised.exception.fallback_allowed)

    def test_invalid_metric_values_fail_closed(self) -> None:
        """布尔值和字符串数字不能穿过平台响应边界。"""

        for value in (True, "125"):
            with self.subTest(value=value):
                payload = self._valid_payload()
                payload["metrics"][0]["trends"][0]["value"] = value
                collector = DouyinDataCollector(
                    session_factory=lambda current=payload: FakeSession(current)
                )
                with self.assertRaises(DouyinDataCollectionError) as raised:
                    collector.collect_direct(self.account)
                self.assertEqual(
                    raised.exception.error_code,
                    "metric_payload_invalid",
                )
                self.assertFalse(raised.exception.fallback_allowed)

    def test_untrusted_transport_error_is_fixed_and_redacted(self) -> None:
        """网络异常原文不得携带 Cookie、URL 或路径离开采集器。"""

        session = FakeSession(
            {},
            error=RuntimeError(
                "Cookie=sessionid=secret "
                "https://creator.douyin.com/?token=secret "
                f"{self.state_file}"
            ),
        )
        collector = DouyinDataCollector(session_factory=lambda: session)

        with self.assertRaises(DouyinDataCollectionError) as raised:
            collector.collect_direct(self.account)

        self.assertEqual(
            raised.exception.error_code,
            "direct_request_rejected",
        )
        self.assertFalse(raised.exception.fallback_allowed)
        self.assertEqual(str(raised.exception), "direct_request_rejected")
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(session.closed, 1)

    def test_registry_rejects_non_douyin_and_non_builtin_platform_ids(self) -> None:
        """注册表不能为未知平台伪造空采集器。"""

        for platform_type in (1, True, "3"):
            with self.subTest(platform_type=platform_type):
                with self.assertRaises(CollectionFailure) as raised:
                    collector_for_platform(platform_type)
                self.assertEqual(
                    raised.exception.error_code,
                    "collector_not_available",
                )


if __name__ == "__main__":
    unittest.main()
