# -*- coding: utf-8 -*-
"""抖音登录会话只读数据采集器测试。"""

from __future__ import annotations

import asyncio
from contextlib import redirect_stderr
from dataclasses import replace
import gc
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app_core.douyin_data_collector import (
    DOUYIN_DASHBOARD_URL,
    DOUYIN_DATA_PAGE_URL,
    DouyinDataCollectionError,
    DouyinDataCollector,
    parse_verified_content_payload,
)
from app_core.platform_data_collection_errors import PlatformDataCollectionError
from app_core.platform_data_collectors import (
    collector_for_platform,
    registered_platform_types,
)
from app_core.platform_data_comment_contract import DouyinCommentContract
from app_core.platform_data_comment_models import CommentInsightFailure
from app_core.platform_data_models import (
    CollectionBatch,
    CollectionFailure,
    MetricPoint,
)


def verified_content_contract() -> DouyinCommentContract:
    return DouyinCommentContract(
        schema_version=1,
        verified=True,
        creator_host="creator.douyin.com",
        content_response_method="GET",
        comment_response_method="GET",
        content_response_path="/verified/content/list",
        content_navigation_template="/verified/content",
        content_list_field="data.items[]",
        content_id_field="data.items[].id",
        content_title_field="data.items[].title",
        content_cover_field="data.items[].cover",
        content_published_at_field="data.items[].published_at",
        content_status_field="data.items[].status",
        content_type_field="data.items[].type",
        content_metric_fields=(
            ("views", "data.items[].metrics.views"),
            ("likes", "data.items[].metrics.likes"),
            ("comments", "data.items[].metrics.comments"),
            ("shares", "data.items[].metrics.shares"),
        ),
        content_cursor_field="data.cursor",
        content_has_more_field="data.has_more",
        comment_response_path="/verified/comment/list",
        comment_navigation_template="/verified/content/{content_id}/comments",
        comment_list_field="data.comments[]",
        comment_id_field="data.comments[].comment_id",
        comment_content_id_field="data.comments[].content_id",
        comment_parent_id_field="data.comments[].parent_id",
        comment_body_field="data.comments[].text",
        comment_like_count_field="data.comments[].like_count",
        comment_reply_count_field="data.comments[].reply_count",
        comment_commented_at_field="data.comments[].commented_at",
        comment_cursor_field="data.cursor",
        comment_has_more_field="data.has_more",
        comment_pagination_trigger="click:verified-comment-load-more",
    )


def valid_content_payload(
    content_id: str = "work-7",
    *,
    cursor: str = "",
    has_more: bool = False,
    **row_overrides: object,
) -> dict:
    row = {
        "id": content_id,
        "title": "作品七",
        "cover": "https://creator.douyin.com/cover/work-7.jpg",
        "published_at": "2026-08-20T12:00:00+08:00",
        "status": "published",
        "type": "video",
        "metrics": {
            "views": 400,
            "likes": 30,
            "comments": 8,
            "shares": 5,
        },
    }
    row.update(row_overrides)
    return {
        "data": {
            "items": [row],
            "cursor": cursor,
            "has_more": has_more,
        }
    }


async def wait_forever_ignoring_cancellation() -> None:
    """模拟不合作的第三方清理协程，取消后仍继续等待。"""

    blocker = asyncio.Event()
    while True:
        try:
            await blocker.wait()
        except asyncio.CancelledError:
            continue


class DouyinDataCollectorTests(unittest.TestCase):
    def test_registry_exposes_only_real_collectors_and_rejects_invalid_types(self) -> None:
        """已登记平台必须都有真实工厂，布尔值和未知值不能被当作平台。"""

        self.assertEqual(registered_platform_types(), (1, 3, 4, 5, 10))
        for platform_type in (True, "1", 2):
            with self.subTest(platform_type=platform_type):
                with self.assertRaises(CollectionFailure) as caught:
                    collector_for_platform(platform_type)
                self.assertEqual(caught.exception.error_code, "collector_not_available")

    def test_registry_builds_douyin_collector(self) -> None:
        """抖音注册项必须创建真实采集器，不能返回空值或占位对象。"""

        collector = collector_for_platform(3)

        self.assertIsInstance(collector, DouyinDataCollector)

    def test_douyin_error_is_platform_neutral_compatible(self) -> None:
        """抖音兼容异常必须可由公共编排层统一捕获。"""

        error = DouyinDataCollectionError(
            "login_required",
            fallback_allowed=False,
        )

        self.assertIsInstance(error, PlatformDataCollectionError)
        self.assertEqual(error.error_code, "login_required")
        self.assertFalse(error.fallback_allowed)


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
            [
                (
                    item.metric_key,
                    item.metric_value,
                    item.metric_scope,
                    item.period_start,
                    item.period_end,
                )
                for item in batch.metrics
            ],
            [
                ("views", 125, "daily_increment", "2026-08-20", "2026-08-20"),
                ("likes", 9, "daily_increment", "2026-08-20", "2026-08-20"),
            ],
        )
        self.assertTrue(batch.account_metrics_available)
        self.assertFalse(batch.content_data_available)
        self.assertEqual(batch.contents, ())
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

    def test_direct_response_uses_one_real_observation_time_for_all_points(
        self,
    ) -> None:
        """同一直连响应的观测时间必须唯一，且不能伪装成统计日零点。"""

        observed_at = "2026-08-20T00:00:01+08:00"
        with patch(
            "app_core.douyin_data_collector._local_observation_timestamp",
            return_value=observed_at,
        ) as observation_clock:
            batch = DouyinDataCollector(
                session_factory=lambda: FakeSession(self._valid_payload())
            ).collect_direct(self.account)

        observation_clock.assert_called_once_with()
        self.assertEqual(batch.platform_observed_at, observed_at)
        self.assertEqual(
            {point.observed_at for point in batch.metrics},
            {observed_at},
        )
        self.assertNotEqual(observed_at, "2026-08-20T00:00:00+08:00")
        self.assertEqual(
            {point.period_end for point in batch.metrics},
            {"2026-08-20"},
        )

    def test_account_batch_marks_content_list_unavailable_without_speculative_request(self) -> None:
        """尚未验证作品接口时，只能保留账户数据，不能猜测补采请求。"""

        session = FakeSession(self._valid_payload())
        batch = DouyinDataCollector(
            session_factory=lambda: session
        ).collect_direct(self.account)

        self.assertTrue(batch.account_metrics_available)
        self.assertFalse(batch.content_data_available)
        self.assertEqual(batch.contents, ())
        self.assertEqual(batch.warning_code, "content_list_unavailable")
        self.assertEqual(
            session.calls,
            [(DOUYIN_DASHBOARD_URL, {"recent_days": 30}, 20.0)],
        )

    def test_all_daily_trend_points_are_preserved(self) -> None:
        """只读取最后一天会丢失历史日趋势，本测试必须失败。"""

        raw_dates = [
            "20260722",
            "20260723",
            "20260724",
            "20260725",
            "20260726",
            "20260727",
            "20260728",
            "20260729",
            "20260730",
            "20260731",
            "20260801",
            "20260802",
            "20260803",
            "20260804",
            "20260805",
            "20260806",
            "20260807",
            "20260808",
            "20260809",
            "20260810",
            "20260811",
            "20260812",
            "20260813",
            "20260814",
            "20260815",
            "20260816",
            "20260817",
            "20260818",
            "20260819",
            "20260820",
        ]
        expected_days = [
            "2026-07-22",
            "2026-07-23",
            "2026-07-24",
            "2026-07-25",
            "2026-07-26",
            "2026-07-27",
            "2026-07-28",
            "2026-07-29",
            "2026-07-30",
            "2026-07-31",
            "2026-08-01",
            "2026-08-02",
            "2026-08-03",
            "2026-08-04",
            "2026-08-05",
            "2026-08-06",
            "2026-08-07",
            "2026-08-08",
            "2026-08-09",
            "2026-08-10",
            "2026-08-11",
            "2026-08-12",
            "2026-08-13",
            "2026-08-14",
            "2026-08-15",
            "2026-08-16",
            "2026-08-17",
            "2026-08-18",
            "2026-08-19",
            "2026-08-20",
        ]
        payload = {
            "status_code": 0,
            "metrics": [
                {
                    "english_metric_name": "play_cnt",
                    "trends": [
                        {"date_time": raw_date, "value": index}
                        for index, raw_date in enumerate(raw_dates, start=1)
                    ],
                }
            ],
        }
        batch = DouyinDataCollector(
            session_factory=lambda: FakeSession(payload)
        ).collect_direct(self.account)

        points = [
            point for point in batch.metrics if point.metric_key == "views"
        ]
        self.assertEqual(len(points), 30)
        self.assertEqual(
            [point.period_start for point in points], expected_days
        )
        self.assertTrue(
            all(point.period_end == point.period_start for point in points)
        )
        self.assertTrue(
            all(point.metric_scope == "daily_increment" for point in points)
        )
        self.assertEqual(
            len(
                {
                    (point.metric_key, point.metric_scope, point.period_start)
                    for point in points
                }
            ),
            30,
        )

    def test_daily_trend_rejects_invalid_or_duplicate_dates(self) -> None:
        """伪日期、重复日和弱类型数值都不能伪装成日趋势。"""

        invalid_cases = {
            "missing_date": [
                {"value": 1},
            ],
            "invalid_date": [
                {"date_time": "20260230", "value": 1},
            ],
            "duplicate_date": [
                {"date_time": "20260820", "value": 1},
                {"date_time": "20260820", "value": 2},
            ],
            "boolean_value": [
                {"date_time": "20260820", "value": True},
            ],
            "numeric_string": [
                {"date_time": "20260820", "value": "1"},
            ],
        }

        for name, trends in invalid_cases.items():
            with self.subTest(name=name):
                payload = {
                    "status_code": 0,
                    "metrics": [
                        {
                            "english_metric_name": "play_cnt",
                            "trends": trends,
                        }
                    ],
                }
                collector = DouyinDataCollector(
                    session_factory=lambda current=payload: FakeSession(current)
                )

                with self.assertRaises(DouyinDataCollectionError) as raised:
                    collector.collect_direct(self.account)

                self.assertEqual(
                    raised.exception.error_code, "metric_payload_invalid"
                )
                self.assertFalse(raised.exception.fallback_allowed)
                self.assertIsNone(raised.exception.__cause__)

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

    def test_registry_rejects_unregistered_and_non_builtin_platform_ids(self) -> None:
        """注册表不能为未知平台伪造空采集器。"""

        for platform_type in (2, True, "3"):
            with self.subTest(platform_type=platform_type):
                with self.assertRaises(CollectionFailure) as raised:
                    collector_for_platform(platform_type)
                self.assertEqual(
                    raised.exception.error_code,
                    "collector_not_available",
                )


class DouyinVerifiedContentParserTests(unittest.TestCase):
    observed_at = "2026-08-23T10:21:00+08:00"

    def test_verified_payload_preserves_stable_fields_and_cumulative_metrics(
        self,
    ) -> None:
        """错读合同字段会丢作品稳定 ID，或把累计指标写成账号日增量。"""

        contents, points, cursor = parse_verified_content_payload(
            verified_content_contract(),
            valid_content_payload(cursor="cursor-1", has_more=True),
            account_id=12,
            observed_at=self.observed_at,
        )

        self.assertEqual(cursor, "cursor-1")
        self.assertEqual(
            [
                (
                    item.content_id,
                    item.title,
                    item.cover_url,
                    item.published_at,
                    item.content_status,
                    item.content_type,
                )
                for item in contents
            ],
            [
                (
                    "work-7",
                    "作品七",
                    "https://creator.douyin.com/cover/work-7.jpg",
                    "2026-08-20T12:00:00+08:00",
                    "published",
                    "video",
                )
            ],
        )
        self.assertEqual(
            [
                (
                    point.entity_type,
                    point.entity_key,
                    point.metric_key,
                    point.raw_metric_key,
                    point.metric_value,
                    point.metric_scope,
                )
                for point in points
            ],
            [
                ("content", "work-7", "views", "data.items[].metrics.views", 400, "lifetime_total"),
                ("content", "work-7", "likes", "data.items[].metrics.likes", 30, "lifetime_total"),
                ("content", "work-7", "comments", "data.items[].metrics.comments", 8, "lifetime_total"),
                ("content", "work-7", "shares", "data.items[].metrics.shares", 5, "lifetime_total"),
            ],
        )
        self.assertEqual({point.period_end for point in points}, {"2026-08-23"})
        self.assertEqual({point.observed_at for point in points}, {self.observed_at})

    def test_missing_or_unknown_values_remain_unknown_instead_of_zero(self) -> None:
        """缺失元数据与未知指标不能被补成空业务事实或 0。"""

        payload = valid_content_payload(
            status="platform-specific-status",
            type="platform-specific-type",
            metrics={
                "views": 9,
                "likes": None,
                "comments": "unknown",
                "shares": True,
                "private_metric": 999,
            },
        )
        del payload["data"]["items"][0]["title"]
        del payload["data"]["items"][0]["cover"]
        del payload["data"]["items"][0]["published_at"]

        contents, points, cursor = parse_verified_content_payload(
            verified_content_contract(),
            payload,
            account_id=12,
            observed_at=self.observed_at,
        )

        self.assertEqual(cursor, "")
        self.assertEqual(len(contents), 1)
        self.assertEqual(contents[0].title, "")
        self.assertEqual(contents[0].cover_url, "")
        self.assertEqual(contents[0].published_at, "")
        self.assertEqual(contents[0].content_status, "unavailable")
        self.assertEqual(contents[0].content_type, "unavailable")
        self.assertEqual(
            [(point.metric_key, point.metric_value) for point in points],
            [("views", 9)],
        )
        self.assertNotIn(0, [point.metric_value for point in points])

    def test_duplicate_content_ids_dedupe_identical_rows_and_reject_conflicts(
        self,
    ) -> None:
        """同页重复 ID 只能合并完全相同记录，不能静默覆盖冲突作品。"""

        payload = valid_content_payload()
        payload["data"]["items"].append(
            json.loads(json.dumps(payload["data"]["items"][0]))
        )
        contents, points, _cursor = parse_verified_content_payload(
            verified_content_contract(),
            payload,
            account_id=12,
            observed_at=self.observed_at,
        )
        self.assertEqual([item.content_id for item in contents], ["work-7"])
        self.assertEqual(len(points), 4)

        payload["data"]["items"][1]["title"] = "冲突标题"
        with self.assertRaises(DouyinDataCollectionError) as raised:
            parse_verified_content_payload(
                verified_content_contract(),
                payload,
                account_id=12,
                observed_at=self.observed_at,
            )
        self.assertEqual(raised.exception.error_code, "content_payload_invalid")

    def test_more_pages_require_a_strict_nonempty_cursor(self) -> None:
        """has_more 不能在没有合同游标时诱导采集器猜测下一页。"""

        for cursor in (None, "", True, " cursor "):
            with self.subTest(cursor=cursor):
                with self.assertRaises(DouyinDataCollectionError) as raised:
                    parse_verified_content_payload(
                        verified_content_contract(),
                        valid_content_payload(cursor=cursor, has_more=True),
                        account_id=12,
                        observed_at=self.observed_at,
                    )
                self.assertEqual(
                    raised.exception.error_code, "content_payload_invalid"
                )

    def test_invalid_stable_id_or_custom_container_fails_closed(self) -> None:
        """弱类型 ID 和自定义容器不能穿过已验证 JSON 合同。"""

        class CustomDict(dict):
            pass

        for payload in (
            valid_content_payload(content_id=True),
            CustomDict(valid_content_payload()),
        ):
            with self.subTest(payload_type=type(payload).__name__):
                with self.assertRaises(DouyinDataCollectionError) as raised:
                    parse_verified_content_payload(
                        verified_content_contract(),
                        payload,
                        account_id=12,
                        observed_at=self.observed_at,
                    )
                self.assertEqual(
                    raised.exception.error_code, "content_payload_invalid"
                )

    def test_verified_payload_bounds_rows_containers_and_external_text(self) -> None:
        """单页行数、容器宽度、ID、展示文本和游标都必须有硬上限。"""

        too_many_rows = valid_content_payload()
        row = too_many_rows["data"]["items"][0]
        too_many_rows["data"]["items"] = [
            json.loads(json.dumps(row)) for _index in range(257)
        ]
        wide_row = valid_content_payload()
        wide_row["data"]["items"][0].update(
            {f"extra_{index}": index for index in range(64)}
        )
        cases = (
            too_many_rows,
            wide_row,
            valid_content_payload(content_id="x" * 513),
            valid_content_payload(title="x" * 4_097),
            valid_content_payload(cover="x" * 8_193),
            valid_content_payload(published_at="x" * 129),
            valid_content_payload(status="x" * 65),
            valid_content_payload(type="x" * 65),
            valid_content_payload(cursor="x" * 2_049, has_more=True),
        )

        for payload in cases:
            with self.subTest(case_index=cases.index(payload)):
                with self.assertRaises(DouyinDataCollectionError) as raised:
                    parse_verified_content_payload(
                        verified_content_contract(),
                        payload,
                        account_id=12,
                        observed_at=self.observed_at,
                    )
                self.assertEqual(
                    raised.exception.error_code, "content_payload_invalid"
                )

    def test_negative_cumulative_content_metric_is_rejected(self) -> None:
        """累计作品指标不能接受有限负数并当作可信平台事实。"""

        for value in (-1, -0.5):
            with self.subTest(value=value):
                payload = valid_content_payload()
                payload["data"]["items"][0]["metrics"]["views"] = value

                with self.assertRaises(DouyinDataCollectionError) as raised:
                    parse_verified_content_payload(
                        verified_content_contract(),
                        payload,
                        account_id=12,
                        observed_at=self.observed_at,
                    )

                self.assertEqual(
                    raised.exception.error_code, "content_payload_invalid"
                )

    def test_account_metric_keys_cannot_be_mapped_as_content_metrics(self) -> None:
        """账号粉丝或主页访问量不能借合同字段被伪装成作品指标。"""

        for metric_key in (
            "followers_total",
            "followers_net",
            "profile_visits",
        ):
            with self.subTest(metric_key=metric_key):
                contract = replace(
                    verified_content_contract(),
                    content_metric_fields=(
                        (metric_key, "data.items[].metrics.views"),
                    ),
                )

                with self.assertRaises(DouyinDataCollectionError) as raised:
                    parse_verified_content_payload(
                        contract,
                        valid_content_payload(),
                        account_id=12,
                        observed_at=self.observed_at,
                    )

                self.assertEqual(
                    raised.exception.error_code, "content_payload_invalid"
                )


class FakeBrowserResponse:
    def __init__(self, url: str, payload: object) -> None:
        self.url = url
        self._payload = payload

    async def json(self) -> object:
        return self._payload


class FakeBrowserPage:
    def __init__(
        self,
        response: FakeBrowserResponse | None,
        *,
        final_url: str = "https://creator.douyin.com/creator-micro/home",
    ) -> None:
        self.response = response
        self.url = final_url
        self.listeners: dict[str, object] = {}
        self.goto_calls: list[tuple[str, str, float]] = []
        self.closed = 0

    def on(self, event: str, callback) -> None:
        self.listeners[event] = callback

    async def goto(
        self,
        url: str,
        *,
        wait_until: str,
        timeout: float,
    ) -> None:
        self.goto_calls.append((url, wait_until, timeout))
        if self.response is not None:
            self.listeners["response"](self.response)
            await asyncio.sleep(0)

    async def close(self) -> None:
        self.closed += 1


class FakeBrowserContext:
    def __init__(
        self,
        page: FakeBrowserPage,
        *,
        close_error: BaseException | None = None,
    ) -> None:
        self.page = page
        self.close_error = close_error
        self.storage_states: list[str] = []
        self.closed = 0

    async def new_page(self) -> FakeBrowserPage:
        return self.page

    async def close(self) -> None:
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


class FakeBrowser:
    def __init__(self, context: FakeBrowserContext) -> None:
        self.context = context
        self.launch_context_calls: list[str] = []
        self.closed = 0

    async def new_context(self, *, storage_state: str) -> FakeBrowserContext:
        self.launch_context_calls.append(storage_state)
        return self.context

    async def close(self) -> None:
        self.closed += 1


class FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.launch_calls: list[dict] = []

    async def launch(self, **options) -> FakeBrowser:
        self.launch_calls.append(dict(options))
        return self.browser


class FakePlaywright:
    def __init__(self, browser: FakeBrowser) -> None:
        self.chromium = FakeChromium(browser)
        self.stopped = 0

    async def stop(self) -> None:
        self.stopped += 1


class FakePlaywrightStarter:
    def __init__(self, playwright: FakePlaywright) -> None:
        self.playwright = playwright

    async def start(self) -> FakePlaywright:
        return self.playwright


class FakeContentRequest:
    def __init__(self, method: str) -> None:
        self.method = method


class FakeContentResponse:
    def __init__(
        self,
        url: str,
        payload: object,
        *,
        method: str = "GET",
        status: object = 200,
        raw_headers: object = None,
        raw_body: object = None,
        header_delay: float = 0.0,
        delay: float = 0.0,
        error: BaseException | None = None,
        resist_cancellation: bool = False,
        synchronous_metadata_only: bool = False,
    ) -> None:
        self._url = url
        self._request = FakeContentRequest(method)
        self.status = status
        self.raw_headers = (
            [{"name": "content-type", "value": "application/json"}]
            if raw_headers is None
            else raw_headers
        )
        self.raw_body = raw_body
        self.header_delay = header_delay
        self.payload = payload
        self.delay = delay
        self.error = error
        self.resist_cancellation = resist_cancellation
        self.synchronous_metadata_only = synchronous_metadata_only
        self.metadata_callback_active = False
        self.header_calls = 0
        self.body_calls = 0
        self.json_calls = 0

    @property
    def url(self) -> str:
        if self.synchronous_metadata_only and not self.metadata_callback_active:
            raise AssertionError("response URL must be filtered synchronously")
        return self._url

    @property
    def request(self) -> FakeContentRequest:
        if self.synchronous_metadata_only and not self.metadata_callback_active:
            raise AssertionError("request method must be filtered synchronously")
        return self._request

    async def headers_array(self) -> object:
        self.header_calls += 1
        if self.header_delay:
            await asyncio.sleep(self.header_delay)
        return self.raw_headers

    async def body(self) -> object:
        self.body_calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.resist_cancellation:
            await wait_forever_ignoring_cancellation()
        if self.error is not None:
            raise self.error
        if self.raw_body is not None:
            return self.raw_body
        return json.dumps(
            self.payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    async def json(self) -> object:
        self.json_calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.resist_cancellation:
            await wait_forever_ignoring_cancellation()
        if self.error is not None:
            raise self.error
        return self.payload


class FakeContentPage:
    def __init__(
        self,
        responses: tuple[FakeContentResponse, ...],
        *,
        final_url: str = "https://creator.douyin.com/verified/content",
        close_error: BaseException | None = None,
        goto_delay: float = 0.0,
        goto_error: BaseException | None = None,
        after_responses_delay: float = 0.0,
        close_delay: float = 0.0,
        close_resists_cancellation: bool = False,
    ) -> None:
        self.responses = responses
        self.url = final_url
        self.close_error = close_error
        self.goto_delay = goto_delay
        self.goto_error = goto_error
        self.after_responses_delay = after_responses_delay
        self.close_delay = close_delay
        self.close_resists_cancellation = close_resists_cancellation
        self.listeners: dict[str, object] = {}
        self.listener_removals = 0
        self.goto_calls: list[tuple[str, str, float]] = []
        self.closed = 0

    def on(self, event: str, callback) -> None:
        self.listeners[event] = callback

    def remove_listener(self, event: str, callback) -> None:
        if self.listeners.get(event) is callback:
            self.listeners.pop(event)
        self.listener_removals += 1

    async def goto(
        self,
        url: str,
        *,
        wait_until: str,
        timeout: float,
    ) -> None:
        if "response" not in self.listeners:
            raise AssertionError("response listener must precede navigation")
        self.goto_calls.append((url, wait_until, timeout))
        if self.goto_delay:
            await asyncio.sleep(self.goto_delay)
        if self.goto_error is not None:
            raise self.goto_error
        for response in self.responses:
            response.metadata_callback_active = True
            try:
                self.listeners["response"](response)
            finally:
                response.metadata_callback_active = False
            await asyncio.sleep(0)
        if self.after_responses_delay:
            await asyncio.sleep(self.after_responses_delay)

    async def close(self) -> None:
        self.closed += 1
        if self.close_delay:
            await asyncio.sleep(self.close_delay)
        if self.close_resists_cancellation:
            await wait_forever_ignoring_cancellation()
        if self.close_error is not None:
            raise self.close_error


class FakeContentContext:
    def __init__(
        self,
        page: FakeContentPage,
        *,
        page_delay: float = 0.0,
        page_error: BaseException | None = None,
        close_error: BaseException | None = None,
        close_delay: float = 0.0,
    ) -> None:
        self.page = page
        self.page_delay = page_delay
        self.page_error = page_error
        self.close_error = close_error
        self.close_delay = close_delay
        self.closed = 0

    async def new_page(self) -> FakeContentPage:
        if self.page_delay:
            await asyncio.sleep(self.page_delay)
        if self.page_error is not None:
            raise self.page_error
        return self.page

    async def close(self) -> None:
        self.closed += 1
        if self.close_delay:
            await asyncio.sleep(self.close_delay)
        if self.close_error is not None:
            raise self.close_error


class FakeContentBrowser:
    def __init__(
        self,
        context: FakeContentContext,
        *,
        context_delay: float = 0.0,
        context_error: BaseException | None = None,
        close_delay: float = 0.0,
        close_error: BaseException | None = None,
    ) -> None:
        self.context = context
        self.context_delay = context_delay
        self.context_error = context_error
        self.close_delay = close_delay
        self.close_error = close_error
        self.storage_states: list[str] = []
        self.closed = 0

    async def new_context(self, *, storage_state: str) -> FakeContentContext:
        self.storage_states.append(storage_state)
        if self.context_delay:
            await asyncio.sleep(self.context_delay)
        if self.context_error is not None:
            raise self.context_error
        return self.context

    async def close(self) -> None:
        self.closed += 1
        if self.close_delay:
            await asyncio.sleep(self.close_delay)
        if self.close_error is not None:
            raise self.close_error


class FakeContentChromium:
    def __init__(
        self,
        browser: FakeContentBrowser,
        *,
        launch_delay: float = 0.0,
        launch_error: BaseException | None = None,
    ) -> None:
        self.browser = browser
        self.launch_delay = launch_delay
        self.launch_error = launch_error
        self.launch_calls: list[dict] = []

    async def launch(self, **options) -> FakeContentBrowser:
        self.launch_calls.append(dict(options))
        if self.launch_delay:
            await asyncio.sleep(self.launch_delay)
        if self.launch_error is not None:
            raise self.launch_error
        return self.browser


class FakeContentPlaywright:
    def __init__(
        self,
        browser: FakeContentBrowser,
        *,
        launch_delay: float = 0.0,
        launch_error: BaseException | None = None,
        stop_delay: float = 0.0,
        stop_error: BaseException | None = None,
    ) -> None:
        self.chromium = FakeContentChromium(
            browser,
            launch_delay=launch_delay,
            launch_error=launch_error,
        )
        self.stop_delay = stop_delay
        self.stop_error = stop_error
        self.stopped = 0

    async def stop(self) -> None:
        self.stopped += 1
        if self.stop_delay:
            await asyncio.sleep(self.stop_delay)
        if self.stop_error is not None:
            raise self.stop_error


class FakeContentStarter:
    def __init__(
        self,
        playwright: FakeContentPlaywright,
        *,
        start_delay: float = 0.0,
        start_error: BaseException | None = None,
    ) -> None:
        self.playwright = playwright
        self.start_delay = start_delay
        self.start_error = start_error

    async def start(self) -> FakeContentPlaywright:
        if self.start_delay:
            await asyncio.sleep(self.start_delay)
        if self.start_error is not None:
            raise self.start_error
        return self.playwright


class DouyinContentCompletionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.cookie_dir = Path(self.tempdir.name)
        self.state_file = self.cookie_dir / "oneclick_3_test.json"
        self.state_file.write_text(
            json.dumps({"cookies": [], "origins": []}), encoding="utf-8"
        )
        self.account = {
            "id": 12,
            "type": 3,
            "filePath": self.state_file.name,
        }
        self.cookie_patch = patch(
            "app_core.douyin_data_collector.COOKIE_DIR", self.cookie_dir
        )
        self.cookie_patch.start()

    def tearDown(self) -> None:
        self.cookie_patch.stop()
        self.tempdir.cleanup()

    @staticmethod
    def _account_batch(account_id: int = 12) -> CollectionBatch:
        observed_at = "2026-08-23T10:20:00+08:00"
        return CollectionBatch(
            platform_type=3,
            source_mode="direct_session",
            metrics=(
                MetricPoint(
                    entity_type="account",
                    entity_key=f"account:{account_id}",
                    metric_key="views",
                    raw_metric_key="play",
                    metric_value=125,
                    metric_unit="count",
                    metric_scope="daily_increment",
                    period_start="2026-08-22",
                    period_end="2026-08-22",
                    observed_at=observed_at,
                ),
            ),
            contents=(),
            account_metrics_available=True,
            content_data_available=False,
            platform_observed_at=observed_at,
            warning_code="content_list_unavailable",
        )

    @staticmethod
    def _browser_harness(
        responses: tuple[FakeContentResponse, ...],
        *,
        context_close_error: BaseException | None = None,
    ) -> tuple[
        FakeContentStarter,
        FakeContentPage,
        FakeContentContext,
        FakeContentBrowser,
        FakeContentPlaywright,
    ]:
        page = FakeContentPage(responses)
        context = FakeContentContext(page, close_error=context_close_error)
        browser = FakeContentBrowser(context)
        playwright = FakeContentPlaywright(browser)
        return FakeContentStarter(playwright), page, context, browser, playwright

    @staticmethod
    def _run_public_call_with_wall_limit(
        callback,
        *,
        wall_limit: float = 0.15,
    ) -> tuple[dict[str, object], float, str, bool]:
        outcome: dict[str, object] = {}

        def run() -> None:
            try:
                outcome["value"] = callback()
            except BaseException as exc:
                outcome["error"] = exc

        runner = threading.Thread(
            target=run,
            name="douyin-content-bounded-call",
            daemon=True,
        )
        diagnostics = io.StringIO()
        started_at = time.monotonic()
        with redirect_stderr(diagnostics):
            runner.start()
            runner.join(wall_limit)
            elapsed = time.monotonic() - started_at
            gc.collect()
        return outcome, elapsed, diagnostics.getvalue(), runner.is_alive()

    def test_no_manifest_returns_original_batch_without_starting_a_session(
        self,
    ) -> None:
        """删掉无合同早退会额外打开浏览器，并伪造作品列表能力。"""

        playwright_calls = 0

        def missing_contract() -> DouyinCommentContract:
            raise CommentInsightFailure("comment_content_unavailable")

        def forbidden_playwright() -> object:
            nonlocal playwright_calls
            playwright_calls += 1
            raise AssertionError("no verified contract, no browser")

        collector = DouyinDataCollector(
            contract_loader=missing_contract,
            playwright_factory=forbidden_playwright,
        )
        original = self._account_batch()

        completed = collector.complete_content_data(self.account, original)

        self.assertIs(completed, original)
        self.assertEqual(completed.warning_code, "content_list_unavailable")
        self.assertEqual(playwright_calls, 0)

    def test_verified_completion_matches_exact_response_and_ignores_comments(
        self,
    ) -> None:
        """放宽 host/path/method 或读取评论响应，都会越过已验证作品合同。"""

        ignored = (
            FakeContentResponse(
                "https://evil.example/verified/content/list",
                valid_content_payload("evil-host"),
            ),
            FakeContentResponse(
                "https://creator.douyin.com/unverified/content/list",
                valid_content_payload("wrong-path"),
            ),
            FakeContentResponse(
                "https://creator.douyin.com/verified/content/list",
                valid_content_payload("wrong-method"),
                method="POST",
            ),
            FakeContentResponse(
                "https://creator.douyin.com/verified/comment/list",
                {"data": {"comments": [{"text": "must-not-read"}]}},
            ),
        )
        first_page = FakeContentResponse(
            "https://creator.douyin.com/verified/content/list?cursor=private",
            valid_content_payload("work-7", cursor="cursor-1", has_more=True),
        )
        second_page = FakeContentResponse(
            "https://creator.douyin.com/verified/content/list?cursor=private-2",
            valid_content_payload("work-8", has_more=False),
        )
        starter, page, context, browser, playwright = self._browser_harness(
            (*ignored, first_page, second_page)
        )
        collector = DouyinDataCollector(
            contract_loader=verified_content_contract,
            playwright_factory=lambda: starter,
            browser_timeout_seconds=0.2,
        )

        completed = collector.complete_content_data(
            self.account, self._account_batch()
        )

        self.assertTrue(completed.content_data_available)
        self.assertEqual(completed.warning_code, "")
        self.assertEqual(completed.source_mode, "browser_signed")
        self.assertEqual(
            [item.content_id for item in completed.contents],
            ["work-7", "work-8"],
        )
        self.assertEqual(
            {
                point.metric_key
                for point in completed.metrics
                if point.entity_type == "content"
            },
            {"views", "likes", "comments", "shares"},
        )
        self.assertEqual(
            [response.json_calls for response in ignored], [0, 0, 0, 0]
        )
        self.assertEqual((first_page.json_calls, second_page.json_calls), (0, 0))
        self.assertEqual((first_page.body_calls, second_page.body_calls), (1, 1))
        self.assertEqual(
            page.goto_calls,
            [
                (
                    "https://creator.douyin.com/verified/content",
                    "domcontentloaded",
                    200.0,
                )
            ],
        )
        self.assertEqual((page.closed, context.closed, browser.closed), (1, 1, 1))
        self.assertEqual(playwright.stopped, 1)

    def test_unmatched_response_flood_is_filtered_before_async_work(self) -> None:
        """未命中响应如果被排队，大量广告或评论接口会挤占作品队列。"""

        ignored = tuple(
            FakeContentResponse(
                f"https://creator.douyin.com/unmatched/{index}",
                {"ignored": index},
                synchronous_metadata_only=True,
            )
            for index in range(100)
        )
        accepted = FakeContentResponse(
            "https://creator.douyin.com/verified/content/list",
            valid_content_payload("official-work"),
        )
        starter, page, context, browser, playwright = self._browser_harness(
            (*ignored, accepted)
        )
        collector = DouyinDataCollector(
            contract_loader=verified_content_contract,
            playwright_factory=lambda: starter,
            browser_timeout_seconds=0.2,
        )

        completed = collector.complete_content_data(
            self.account, self._account_batch()
        )

        self.assertEqual(
            [content.content_id for content in completed.contents],
            ["official-work"],
        )
        self.assertTrue(all(response.header_calls == 0 for response in ignored))
        self.assertTrue(all(response.body_calls == 0 for response in ignored))
        self.assertEqual(page.listener_removals, 1)
        self.assertEqual((page.closed, context.closed, browser.closed), (1, 1, 1))
        self.assertEqual(playwright.stopped, 1)

    def test_matched_response_requires_builtin_2xx_status(self) -> None:
        """非 2xx 或弱类型状态不得进入响应体解析。"""

        for status in (199, 300, True, "200"):
            with self.subTest(status=status):
                response = FakeContentResponse(
                    "https://creator.douyin.com/verified/content/list",
                    valid_content_payload(),
                    status=status,
                )
                starter, page, context, browser, playwright = (
                    self._browser_harness((response,))
                )
                collector = DouyinDataCollector(
                    contract_loader=verified_content_contract,
                    playwright_factory=lambda starter=starter: starter,
                    browser_timeout_seconds=0.2,
                )

                with self.assertRaises(DouyinDataCollectionError) as raised:
                    collector.complete_content_data(
                        self.account, self._account_batch()
                    )

                self.assertEqual(
                    raised.exception.error_code, "content_payload_invalid"
                )
                self.assertEqual(response.body_calls, 0)
                self.assertEqual(page.listener_removals, 1)
                self.assertEqual(
                    (page.closed, context.closed, browser.closed), (1, 1, 1)
                )
                self.assertEqual(playwright.stopped, 1)

    def test_matched_response_requires_one_unfolded_json_content_type(self) -> None:
        """缺失、非 JSON、重复或折行 Content-Type 都必须在读取 body 前失败。"""

        cases = (
            [],
            [{"name": "content-type", "value": "text/html"}],
            [
                {"name": "content-type", "value": "application/json"},
                {"name": "Content-Type", "value": "application/json"},
            ],
            [
                {
                    "name": "content-type",
                    "value": "application/json\r\n application/json",
                }
            ],
            [{"name": "content-type", "value": "application/json; charset"}],
        )
        for raw_headers in cases:
            with self.subTest(raw_headers=raw_headers):
                response = FakeContentResponse(
                    "https://creator.douyin.com/verified/content/list",
                    valid_content_payload(),
                    raw_headers=raw_headers,
                )
                starter, page, context, browser, playwright = (
                    self._browser_harness((response,))
                )
                collector = DouyinDataCollector(
                    contract_loader=verified_content_contract,
                    playwright_factory=lambda starter=starter: starter,
                    browser_timeout_seconds=0.2,
                )

                with self.assertRaises(DouyinDataCollectionError) as raised:
                    collector.complete_content_data(
                        self.account, self._account_batch()
                    )

                self.assertEqual(
                    raised.exception.error_code, "content_payload_invalid"
                )
                self.assertEqual(response.body_calls, 0)
                self.assertEqual(page.listener_removals, 1)
                self.assertEqual(
                    (page.closed, context.closed, browser.closed), (1, 1, 1)
                )
                self.assertEqual(playwright.stopped, 1)

    def test_header_and_matched_response_queues_fail_closed_on_overflow(self) -> None:
        """头校验或已匹配 body 队列都不得无界积压。"""

        cases = (
            tuple(
                FakeContentResponse(
                    "https://creator.douyin.com/verified/content/list",
                    valid_content_payload(f"header-{index}"),
                    header_delay=0.05,
                )
                for index in range(9)
            ),
            tuple(
                FakeContentResponse(
                    "https://creator.douyin.com/verified/content/list",
                    valid_content_payload(f"queue-{index}"),
                    delay=0.05 if index == 0 else 0.0,
                )
                for index in range(10)
            ),
        )
        for responses in cases:
            with self.subTest(first_content_id=responses[0].payload["data"]["items"][0]["id"]):
                starter, page, context, browser, playwright = (
                    self._browser_harness(responses)
                )
                collector = DouyinDataCollector(
                    contract_loader=verified_content_contract,
                    playwright_factory=lambda starter=starter: starter,
                    browser_timeout_seconds=0.2,
                )

                with self.assertRaises(DouyinDataCollectionError) as raised:
                    collector.complete_content_data(
                        self.account, self._account_batch()
                    )

                self.assertEqual(
                    raised.exception.error_code, "content_payload_invalid"
                )
                self.assertEqual(page.listener_removals, 1)
                self.assertEqual(
                    (page.closed, context.closed, browser.closed), (1, 1, 1)
                )
                self.assertEqual(playwright.stopped, 1)

    def test_response_body_and_cumulative_bytes_are_bounded(self) -> None:
        """单个响应体和整次分页的原始字节都不得无界累积。"""

        oversized_payload = valid_content_payload()
        oversized_payload["padding"] = "x" * 1_048_576
        oversized = (
            FakeContentResponse(
                "https://creator.douyin.com/verified/content/list",
                oversized_payload,
            ),
        )
        cumulative = tuple(
            FakeContentResponse(
                "https://creator.douyin.com/verified/content/list",
                {
                    **valid_content_payload(
                        f"work-{index}",
                        cursor=f"cursor-{index}" if index < 4 else "",
                        has_more=index < 4,
                    ),
                    "padding": "x" * 900_000,
                },
            )
            for index in range(5)
        )

        for responses in (oversized, cumulative):
            with self.subTest(response_count=len(responses)):
                starter, page, context, browser, playwright = (
                    self._browser_harness(responses)
                )
                collector = DouyinDataCollector(
                    contract_loader=verified_content_contract,
                    playwright_factory=lambda starter=starter: starter,
                    browser_timeout_seconds=0.5,
                )

                with self.assertRaises(DouyinDataCollectionError) as raised:
                    collector.complete_content_data(
                        self.account, self._account_batch()
                    )

                self.assertEqual(
                    raised.exception.error_code, "content_payload_invalid"
                )
                self.assertEqual(page.listener_removals, 1)
                self.assertEqual(
                    (page.closed, context.closed, browser.closed), (1, 1, 1)
                )
                self.assertEqual(playwright.stopped, 1)

    def test_cumulative_works_metrics_pages_and_cursors_are_bounded(self) -> None:
        """即使每页合法，整次任务的作品、指标、页数和游标也必须有上限。"""

        def rows_payload(
            start: int,
            count: int,
            *,
            cursor: str,
            has_more: bool,
        ) -> dict:
            payload = valid_content_payload(
                f"work-{start}", cursor=cursor, has_more=has_more
            )
            template = payload["data"]["items"][0]
            rows = []
            for index in range(start, start + count):
                candidate = json.loads(json.dumps(template))
                candidate["id"] = f"work-{index}"
                rows.append(candidate)
            payload["data"]["items"] = rows
            return payload

        one_metric_contract = replace(
            verified_content_contract(),
            content_metric_fields=(
                ("views", "data.items[].metrics.views"),
            ),
        )
        excessive_works = tuple(
            FakeContentResponse(
                "https://creator.douyin.com/verified/content/list",
                rows_payload(
                    start,
                    count,
                    cursor=cursor,
                    has_more=has_more,
                ),
            )
            for start, count, cursor, has_more in (
                (0, 200, "works-1", True),
                (200, 200, "works-2", True),
                (400, 101, "", False),
            )
        )
        excessive_metrics = tuple(
            FakeContentResponse(
                "https://creator.douyin.com/verified/content/list",
                rows_payload(
                    start,
                    188,
                    cursor="metrics-1" if start == 0 else "",
                    has_more=start == 0,
                ),
            )
            for start in (0, 188)
        )
        excessive_pages = tuple(
            FakeContentResponse(
                "https://creator.douyin.com/verified/content/list",
                valid_content_payload(
                    f"page-{index}",
                    cursor=f"cursor-{index}",
                    has_more=True,
                ),
            )
            for index in range(51)
        )

        for contract, responses in (
            (one_metric_contract, excessive_works),
            (verified_content_contract(), excessive_metrics),
            (verified_content_contract(), excessive_pages),
        ):
            with self.subTest(response_count=len(responses)):
                starter, page, context, browser, playwright = (
                    self._browser_harness(responses)
                )
                collector = DouyinDataCollector(
                    contract_loader=lambda contract=contract: contract,
                    playwright_factory=lambda starter=starter: starter,
                    browser_timeout_seconds=1.0,
                )

                with self.assertRaises(DouyinDataCollectionError) as raised:
                    collector.complete_content_data(
                        self.account, self._account_batch()
                    )

                self.assertEqual(
                    raised.exception.error_code, "content_payload_invalid"
                )
                self.assertEqual(page.listener_removals, 1)
                self.assertEqual(
                    (page.closed, context.closed, browser.closed), (1, 1, 1)
                )
                self.assertEqual(playwright.stopped, 1)

    def test_synchronous_projection_cannot_escape_the_operation_deadline(self) -> None:
        """解码、JSON 形状准备或模型投影卡住时，公开调用也必须按总时限返回。"""

        response = FakeContentResponse(
            "https://creator.douyin.com/verified/content/list",
            valid_content_payload(),
        )
        starter, page, context, browser, playwright = self._browser_harness(
            (response,)
        )
        collector = DouyinDataCollector(
            contract_loader=verified_content_contract,
            playwright_factory=lambda: starter,
            browser_timeout_seconds=0.03,
        )
        real_parser = parse_verified_content_payload

        def slow_parser(*args, **kwargs):
            time.sleep(0.06)
            return real_parser(*args, **kwargs)

        started_at = time.monotonic()
        with patch(
            "app_core.douyin_data_collector.parse_verified_content_payload",
            side_effect=slow_parser,
        ):
            with self.assertRaises(DouyinDataCollectionError) as raised:
                collector.complete_content_data(
                    self.account, self._account_batch()
                )
        elapsed = time.monotonic() - started_at

        self.assertIn(
            raised.exception.error_code,
            {"content_list_unavailable", "browser_cleanup_incomplete"},
        )
        self.assertLess(elapsed, 0.06)
        self.assertEqual(page.listener_removals, 1)
        self.assertEqual((page.closed, context.closed, browser.closed), (1, 1, 1))
        self.assertEqual(playwright.stopped, 1)

    def _complete_after_ignored_authority(
        self, ignored_url: str
    ) -> tuple[CollectionBatch, FakeContentResponse]:
        ignored = FakeContentResponse(
            ignored_url,
            valid_content_payload("must-not-be-accepted"),
        )
        accepted = FakeContentResponse(
            "https://creator.douyin.com/verified/content/list",
            valid_content_payload("official-work"),
        )
        starter, _page, _context, _browser, _playwright = (
            self._browser_harness((ignored, accepted))
        )
        collector = DouyinDataCollector(
            contract_loader=verified_content_contract,
            playwright_factory=lambda: starter,
            browser_timeout_seconds=0.2,
        )
        return (
            collector.complete_content_data(
                self.account, self._account_batch()
            ),
            ignored,
        )

    def test_creator_url_with_userinfo_is_not_official_authority(self) -> None:
        """只比较 hostname 会错收带用户信息的非标准 authority。"""

        completed, ignored = self._complete_after_ignored_authority(
            "https://user:private@creator.douyin.com/verified/content/list"
        )

        self.assertEqual(
            [item.content_id for item in completed.contents],
            ["official-work"],
        )
        self.assertEqual(ignored.json_calls, 0)

    def test_creator_url_with_nondefault_port_is_not_official_authority(
        self,
    ) -> None:
        """只比较 hostname 会错收合同从未授权的显式端口。"""

        completed, ignored = self._complete_after_ignored_authority(
            "https://creator.douyin.com:444/verified/content/list"
        )

        self.assertEqual(
            [item.content_id for item in completed.contents],
            ["official-work"],
        )
        self.assertEqual(ignored.json_calls, 0)

    def test_conflicting_duplicate_across_pages_fails_closed_and_cleans(
        self,
    ) -> None:
        """后页同 ID 冲突不能覆盖前页，失败时仍要关闭四层资源。"""

        first_page = FakeContentResponse(
            "https://creator.douyin.com/verified/content/list",
            valid_content_payload(cursor="cursor-1", has_more=True),
        )
        second_page = FakeContentResponse(
            "https://creator.douyin.com/verified/content/list",
            valid_content_payload(title="冲突标题"),
        )
        starter, page, context, browser, playwright = self._browser_harness(
            (first_page, second_page)
        )
        collector = DouyinDataCollector(
            contract_loader=verified_content_contract,
            playwright_factory=lambda: starter,
            browser_timeout_seconds=0.2,
        )

        with self.assertRaises(DouyinDataCollectionError) as raised:
            collector.complete_content_data(self.account, self._account_batch())

        self.assertEqual(raised.exception.error_code, "content_payload_invalid")
        self.assertEqual((page.closed, context.closed, browser.closed), (1, 1, 1))
        self.assertEqual(playwright.stopped, 1)

    def test_cleanup_failure_overrides_apparent_content_success(self) -> None:
        """短会话没有全部关闭时，刚读到的作品不能进入同步。"""

        response = FakeContentResponse(
            "https://creator.douyin.com/verified/content/list",
            valid_content_payload(),
        )
        starter, page, context, browser, playwright = self._browser_harness(
            (response,),
            context_close_error=RuntimeError("private cleanup detail"),
        )
        collector = DouyinDataCollector(
            contract_loader=verified_content_contract,
            playwright_factory=lambda: starter,
            browser_timeout_seconds=0.2,
        )

        with self.assertRaises(DouyinDataCollectionError) as raised:
            collector.complete_content_data(self.account, self._account_batch())

        self.assertEqual(
            raised.exception.error_code, "browser_cleanup_incomplete"
        )
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual((page.closed, context.closed, browser.closed), (1, 1, 1))
        self.assertEqual(playwright.stopped, 1)

    def test_total_deadline_bounds_every_pre_response_stage(self) -> None:
        """启动、建浏览器、建上下文、建页面和导航不能各自无限等待。"""

        for stage in ("startup", "launch", "context", "page", "navigation"):
            with self.subTest(stage=stage):
                response = FakeContentResponse(
                    "https://creator.douyin.com/verified/content/list",
                    valid_content_payload(),
                )
                starter, page, context, browser, playwright = (
                    self._browser_harness((response,))
                )
                if stage == "startup":
                    starter.start_delay = 0.06
                elif stage == "launch":
                    playwright.chromium.launch_delay = 0.06
                elif stage == "context":
                    browser.context_delay = 0.06
                elif stage == "page":
                    context.page_delay = 0.06
                else:
                    page.goto_delay = 0.06
                collector = DouyinDataCollector(
                    contract_loader=verified_content_contract,
                    playwright_factory=lambda starter=starter: starter,
                    browser_timeout_seconds=0.02,
                )

                started_at = time.monotonic()
                with self.assertRaises(DouyinDataCollectionError) as raised:
                    collector.complete_content_data(
                        self.account, self._account_batch()
                    )
                elapsed = time.monotonic() - started_at

                self.assertEqual(
                    raised.exception.error_code, "content_list_unavailable"
                )
                self.assertLess(elapsed, 0.05)

    def test_response_processing_uses_only_remaining_total_deadline(self) -> None:
        """前面阶段用掉的时间不能在等响应时重新计时。"""

        response = FakeContentResponse(
            "https://creator.douyin.com/verified/content/list",
            valid_content_payload(),
            delay=0.03,
        )
        starter, _page, _context, _browser, _playwright = (
            self._browser_harness((response,))
        )
        starter.start_delay = 0.02
        collector = DouyinDataCollector(
            contract_loader=verified_content_contract,
            playwright_factory=lambda: starter,
            browser_timeout_seconds=0.04,
        )

        started_at = time.monotonic()
        with self.assertRaises(DouyinDataCollectionError) as raised:
            collector.complete_content_data(self.account, self._account_batch())
        elapsed = time.monotonic() - started_at

        self.assertEqual(
            raised.exception.error_code, "content_list_unavailable"
        )
        self.assertLess(elapsed, 0.06)

    def test_all_four_close_timeouts_share_bounded_cleanup_reservation(
        self,
    ) -> None:
        """四层资源关闭各自卡住时，仍要全部尝试且按总时限退出。"""

        response = FakeContentResponse(
            "https://creator.douyin.com/verified/content/list",
            valid_content_payload(),
        )
        starter, page, context, browser, playwright = self._browser_harness(
            (response,)
        )
        page.close_delay = 0.04
        context.close_delay = 0.04
        browser.close_delay = 0.04
        playwright.stop_delay = 0.04
        collector = DouyinDataCollector(
            contract_loader=verified_content_contract,
            playwright_factory=lambda: starter,
            browser_timeout_seconds=0.04,
        )

        started_at = time.monotonic()
        with self.assertRaises(DouyinDataCollectionError) as raised:
            collector.complete_content_data(self.account, self._account_batch())
        elapsed = time.monotonic() - started_at

        self.assertEqual(
            raised.exception.error_code, "browser_cleanup_incomplete"
        )
        self.assertLess(elapsed, 0.08)
        self.assertEqual((page.closed, context.closed, browser.closed), (1, 1, 1))
        self.assertEqual(playwright.stopped, 1)

    def test_cancellation_resistant_close_cannot_hang_public_call(self) -> None:
        """关闭协程吞掉取消时，公开入口仍必须有界返回并继续尝试其他关闭。"""

        response = FakeContentResponse(
            "https://creator.douyin.com/verified/content/list",
            valid_content_payload(),
        )
        starter, page, context, browser, playwright = self._browser_harness(
            (response,)
        )
        page.close_resists_cancellation = True
        collector = DouyinDataCollector(
            contract_loader=verified_content_contract,
            playwright_factory=lambda: starter,
            browser_timeout_seconds=0.04,
        )

        outcome, elapsed, diagnostics, still_running = (
            self._run_public_call_with_wall_limit(
                lambda: collector.complete_content_data(
                    self.account, self._account_batch()
                )
            )
        )

        self.assertFalse(still_running, "public call exceeded hard wall limit")
        self.assertLess(elapsed, 0.15)
        self.assertIsInstance(outcome.get("error"), DouyinDataCollectionError)
        error = outcome["error"]
        self.assertEqual(error.error_code, "browser_cleanup_incomplete")
        self.assertEqual(str(error), "browser_cleanup_incomplete")
        self.assertIsNone(error.__cause__)
        self.assertEqual((page.closed, context.closed, browser.closed), (1, 1, 1))
        self.assertEqual(playwright.stopped, 1)
        self.assertEqual(diagnostics, "")

    def test_cancellation_resistant_worker_is_bounded_without_thread_leaks(
        self,
    ) -> None:
        """响应 worker 不承认取消时，重复公开调用不得挂起、泄漏路径或累积后台线程。"""

        initial_thread_count = threading.active_count()
        for invocation in range(3):
            with self.subTest(invocation=invocation):
                response = FakeContentResponse(
                    "https://creator.douyin.com/verified/content/list",
                    valid_content_payload(),
                    resist_cancellation=True,
                )
                starter, page, context, browser, playwright = (
                    self._browser_harness((response,))
                )
                page.url = "https://creator.douyin.com/login"
                page.after_responses_delay = 0.005
                collector = DouyinDataCollector(
                    contract_loader=verified_content_contract,
                    playwright_factory=lambda starter=starter: starter,
                    browser_timeout_seconds=0.04,
                )

                outcome, elapsed, diagnostics, still_running = (
                    self._run_public_call_with_wall_limit(
                        lambda collector=collector: collector.complete_content_data(
                            self.account, self._account_batch()
                        )
                    )
                )

                self.assertFalse(
                    still_running, "public call exceeded hard wall limit"
                )
                self.assertLess(elapsed, 0.15)
                self.assertIsInstance(
                    outcome.get("error"), DouyinDataCollectionError
                )
                error = outcome["error"]
                self.assertEqual(
                    error.error_code, "browser_cleanup_incomplete"
                )
                self.assertEqual(str(error), "browser_cleanup_incomplete")
                self.assertIsNone(error.__cause__)
                self.assertEqual(page.listener_removals, 1)
                self.assertEqual(
                    (page.closed, context.closed, browser.closed), (1, 1, 1)
                )
                self.assertEqual(playwright.stopped, 1)
                self.assertNotIn(str(self.state_file), diagnostics)
                self.assertNotIn("Traceback", diagnostics)
                self.assertNotIn("Task was destroyed", diagnostics)

        self.assertEqual(threading.active_count(), initial_thread_count)

    def test_navigation_process_control_exceptions_survive_cleanup(self) -> None:
        """CancelledError、KeyboardInterrupt 和 SystemExit 不能被改写为业务错误。"""

        for exception_type in (
            asyncio.CancelledError,
            KeyboardInterrupt,
            SystemExit,
        ):
            with self.subTest(exception_type=exception_type.__name__):
                starter, page, context, browser, playwright = (
                    self._browser_harness(())
                )
                page.goto_error = exception_type()
                collector = DouyinDataCollector(
                    contract_loader=verified_content_contract,
                    playwright_factory=lambda starter=starter: starter,
                    browser_timeout_seconds=0.1,
                )

                with self.assertRaises(exception_type):
                    collector.complete_content_data(
                        self.account, self._account_batch()
                    )

                self.assertEqual(
                    (page.closed, context.closed, browser.closed), (1, 1, 1)
                )
                self.assertEqual(playwright.stopped, 1)

    def test_response_cancellation_is_not_remapped(self) -> None:
        """响应解析任务被取消时，调用方必须收到原始取消。"""

        response = FakeContentResponse(
            "https://creator.douyin.com/verified/content/list",
            valid_content_payload(),
            error=asyncio.CancelledError(),
        )
        starter, page, context, browser, playwright = self._browser_harness(
            (response,)
        )
        collector = DouyinDataCollector(
            contract_loader=verified_content_contract,
            playwright_factory=lambda: starter,
            browser_timeout_seconds=0.1,
        )

        with self.assertRaises(asyncio.CancelledError):
            collector.complete_content_data(self.account, self._account_batch())

        self.assertEqual((page.closed, context.closed, browser.closed), (1, 1, 1))
        self.assertEqual(playwright.stopped, 1)

    def test_cleanup_process_control_exceptions_survive_all_close_attempts(
        self,
    ) -> None:
        """关闭阶段的进程控制异常也要保留，但不得阻止其他资源关闭。"""

        for exception_type in (
            asyncio.CancelledError,
            KeyboardInterrupt,
            SystemExit,
        ):
            with self.subTest(exception_type=exception_type.__name__):
                response = FakeContentResponse(
                    "https://creator.douyin.com/verified/content/list",
                    valid_content_payload(),
                )
                starter, page, context, browser, playwright = (
                    self._browser_harness((response,))
                )
                page.close_error = exception_type()
                collector = DouyinDataCollector(
                    contract_loader=verified_content_contract,
                    playwright_factory=lambda starter=starter: starter,
                    browser_timeout_seconds=0.1,
                )

                with self.assertRaises(exception_type):
                    collector.complete_content_data(
                        self.account, self._account_batch()
                    )

                self.assertEqual(
                    (page.closed, context.closed, browser.closed), (1, 1, 1)
                )
                self.assertEqual(playwright.stopped, 1)

    def test_early_exit_removes_listener_and_consumes_future_exception(
        self,
    ) -> None:
        """登录、验证或拒绝访问早退不得留下未取回 Future 异常或私密路径。"""

        cases = (
            ("https://creator.douyin.com/verification", "verification_required"),
            ("https://creator.douyin.com/login", "login_required"),
            ("https://creator.douyin.com/forbidden", "metric_payload_invalid"),
        )
        for final_url, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                response = FakeContentResponse(
                    "https://creator.douyin.com/verified/content/list",
                    valid_content_payload(),
                    error=RuntimeError(f"private path: {self.state_file}"),
                )
                starter, page, _context, _browser, _playwright = (
                    self._browser_harness((response,))
                )
                page.url = final_url
                collector = DouyinDataCollector(
                    contract_loader=verified_content_contract,
                    playwright_factory=lambda: starter,
                    browser_timeout_seconds=0.1,
                )

                async def exercise() -> tuple[str, int, list[dict]]:
                    loop = asyncio.get_running_loop()
                    events: list[dict] = []
                    loop.set_exception_handler(
                        lambda _loop, context: events.append(dict(context))
                    )
                    error_code = ""
                    try:
                        await collector._complete_content_data_async(
                            self.account,
                            self._account_batch(),
                            verified_content_contract(),
                            None,
                        )
                    except DouyinDataCollectionError as exc:
                        error_code = exc.error_code
                    removals = page.listener_removals
                    page.listeners.clear()
                    gc.collect()
                    await asyncio.sleep(0)
                    return error_code, removals, events

                error_code, removals, events = asyncio.run(exercise())

                self.assertEqual(error_code, expected_code)
                self.assertEqual(removals, 1)
                self.assertEqual(events, [])

    def test_late_cross_account_results_stay_with_their_invocation(self) -> None:
        """共用采集器的旧账号慢响应不能覆盖新账号的作品批次。"""

        second_state = self.cookie_dir / "oneclick_3_second.json"
        second_state.write_text(
            json.dumps({"cookies": [], "origins": []}), encoding="utf-8"
        )
        second_account = {
            "id": 13,
            "type": 3,
            "filePath": second_state.name,
        }
        first_starter, *_first_resources = self._browser_harness(
            (
                FakeContentResponse(
                    "https://creator.douyin.com/verified/content/list",
                    valid_content_payload("account-12-work"),
                    delay=0.03,
                ),
            )
        )
        second_starter, *_second_resources = self._browser_harness(
            (
                FakeContentResponse(
                    "https://creator.douyin.com/verified/content/list",
                    valid_content_payload("account-13-work"),
                ),
            )
        )
        starters = {
            "account-12": first_starter,
            "account-13": second_starter,
        }
        collector = DouyinDataCollector(
            contract_loader=verified_content_contract,
            playwright_factory=lambda: starters[threading.current_thread().name],
            browser_timeout_seconds=0.2,
        )
        results: dict[int, CollectionBatch] = {}
        errors: list[BaseException] = []

        def run(account: dict, batch: CollectionBatch) -> None:
            try:
                results[account["id"]] = collector.complete_content_data(
                    account, batch
                )
            except BaseException as error:
                errors.append(error)

        first_thread = threading.Thread(
            target=run,
            args=(self.account, self._account_batch(12)),
            name="account-12",
        )
        second_thread = threading.Thread(
            target=run,
            args=(second_account, self._account_batch(13)),
            name="account-13",
        )
        first_thread.start()
        second_thread.start()
        first_thread.join(1)
        second_thread.join(1)

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(
            [item.content_id for item in results[12].contents],
            ["account-12-work"],
        )
        self.assertEqual(
            [item.content_id for item in results[13].contents],
            ["account-13-work"],
        )
        self.assertIn(
            "account:12",
            {point.entity_key for point in results[12].metrics},
        )
        self.assertNotIn(
            "account:12",
            {point.entity_key for point in results[13].metrics},
        )


class DouyinBrowserSignedCollectorTests(DouyinDirectCollectorTests):
    @staticmethod
    def _current_overview_payload() -> dict:
        return {
            "status_code": 0,
            "status_message": "",
            "data": {
                "play": {
                    "status_code": 0,
                    "current_count": 230,
                    "option_list": [
                        {"date": "20260819", "count": 100},
                        {"date": "20260820", "count": 230},
                    ],
                },
                "digg": {
                    "status_code": 0,
                    "current_count": 18,
                    "option_list": [{"date": "20260820", "count": 18}],
                },
                "private_metric": {
                    "status_code": 0,
                    "current_count": 999,
                },
            },
        }

    def _collector_with_browser(
        self,
        response: FakeBrowserResponse | None,
        *,
        final_url: str = "https://creator.douyin.com/creator-micro/home",
        close_error: BaseException | None = None,
        timeout: float = 0.05,
    ):
        page = FakeBrowserPage(response, final_url=final_url)
        context = FakeBrowserContext(page, close_error=close_error)
        browser = FakeBrowser(context)
        playwright = FakePlaywright(browser)
        collector = DouyinDataCollector(
            session_factory=lambda: FakeSession({}),
            playwright_factory=lambda: FakePlaywrightStarter(playwright),
            browser_timeout_seconds=timeout,
        )
        return collector, page, context, browser, playwright

    def test_browser_option_list_preserves_daily_scope_and_proven_total(self) -> None:
        """浏览器趋势必须逐日保留，只有带日期的 fans 当前数才是总粉丝。"""

        payload = {
            "status_code": 0,
            "data": {
                "play": {
                    "status_code": 0,
                    "current_count": 999,
                    "option_list": [
                        {"date": "2026-08-19", "count": 100},
                        {"date": "2026-08-20", "count": 230},
                    ],
                },
                "fans": {
                    "status_code": 0,
                    "current_count": 431,
                    "option_list": [
                        {"date": "2026-08-19", "count": 11},
                        {"date": "2026-08-20", "count": 12},
                    ],
                },
                "digg": {
                    "status_code": 0,
                    "current_count": 18,
                },
            },
        }
        response = FakeBrowserResponse(
            "https://creator.douyin.com/aweme/janus/creator/data/overview/all/",
            payload,
        )
        collector, _page, _context, _browser, _playwright = (
            self._collector_with_browser(response)
        )

        batch = collector.collect_browser_signed(self.account)

        self.assertEqual(
            [
                (
                    point.metric_key,
                    point.metric_value,
                    point.metric_scope,
                    point.period_start,
                    point.period_end,
                )
                for point in batch.metrics
            ],
            [
                ("views", 100, "daily_increment", "2026-08-19", "2026-08-19"),
                ("views", 230, "daily_increment", "2026-08-20", "2026-08-20"),
                (
                    "followers_net",
                    11,
                    "daily_increment",
                    "2026-08-19",
                    "2026-08-19",
                ),
                (
                    "followers_net",
                    12,
                    "daily_increment",
                    "2026-08-20",
                    "2026-08-20",
                ),
                (
                    "followers_total",
                    431,
                    "lifetime_total",
                    "2026-08-20",
                    "2026-08-20",
                ),
            ],
        )
        self.assertTrue(batch.account_metrics_available)
        self.assertFalse(batch.content_data_available)
        self.assertEqual(batch.contents, ())
        self.assertEqual(
            len(
                {
                    (point.metric_key, point.metric_scope, point.period_start)
                    for point in batch.metrics
                }
            ),
            len(batch.metrics),
        )

    def test_browser_response_uses_one_real_observation_time_for_all_points(
        self,
    ) -> None:
        """同一签名响应中的日趋势与粉丝总数必须共用实际观测时间。"""

        payload = self._current_overview_payload()
        payload["data"]["fans"] = {
            "status_code": 0,
            "current_count": 431,
            "option_list": [{"date": "20260820", "count": 12}],
        }
        response = FakeBrowserResponse(
            "https://creator.douyin.com/aweme/janus/creator/data/overview/all/",
            payload,
        )
        collector, _page, _context, _browser, _playwright = (
            self._collector_with_browser(response)
        )
        observed_at = "2026-08-20T12:34:56+08:00"

        with patch(
            "app_core.douyin_data_collector._local_observation_timestamp",
            return_value=observed_at,
        ) as observation_clock:
            batch = collector.collect_browser_signed(self.account)

        observation_clock.assert_called_once_with()
        self.assertEqual(batch.platform_observed_at, observed_at)
        self.assertEqual(
            {point.observed_at for point in batch.metrics},
            {observed_at},
        )
        self.assertFalse(
            any(point.observed_at.endswith("T00:00:00+08:00") for point in batch.metrics)
        )
        self.assertIn(
            ("followers_total", "2026-08-20"),
            {(point.metric_key, point.period_end) for point in batch.metrics},
        )

    def test_browser_signed_accepts_only_allowlisted_official_response_and_closes(self) -> None:
        """官方响应路径或严格关闭缺失时，本测试必须失败。"""

        response = FakeBrowserResponse(
            "https://creator.douyin.com/aweme/janus/creator/data/overview/all/"
            "?msToken=private",
            self._current_overview_payload(),
        )
        collector, page, context, browser, playwright = (
            self._collector_with_browser(response)
        )

        batch = collector.collect_browser_signed(self.account)

        self.assertEqual(batch.source_mode, "browser_signed")
        self.assertEqual(batch.warning_code, "content_list_unavailable")
        self.assertEqual(
            [
                (
                    point.metric_key,
                    point.metric_value,
                    point.metric_scope,
                    point.period_start,
                    point.period_end,
                )
                for point in batch.metrics
            ],
            [
                ("views", 100, "daily_increment", "2026-08-19", "2026-08-19"),
                ("views", 230, "daily_increment", "2026-08-20", "2026-08-20"),
                ("likes", 18, "daily_increment", "2026-08-20", "2026-08-20"),
            ],
        )
        self.assertEqual(page.closed, 1)
        self.assertEqual(context.closed, 1)
        self.assertEqual(browser.closed, 1)
        self.assertEqual(playwright.stopped, 1)
        self.assertEqual(playwright.chromium.launch_calls, [{"headless": True}])
        self.assertEqual(
            page.goto_calls,
            [(DOUYIN_DATA_PAGE_URL, "domcontentloaded", 50.0)],
        )

    def test_non_allowlisted_response_times_out_without_being_parsed(self) -> None:
        """同域未知接口不能被当作数据指标来源。"""

        response = FakeBrowserResponse(
            "https://creator.douyin.com/web/api/media/aweme/create/",
            self._current_overview_payload(),
        )
        collector, _page, _context, _browser, _playwright = (
            self._collector_with_browser(response, timeout=0.01)
        )

        with self.assertRaises(DouyinDataCollectionError) as raised:
            collector.collect_browser_signed(self.account)

        self.assertEqual(
            raised.exception.error_code,
            "browser_signature_timeout",
        )
        self.assertIsNone(raised.exception.__cause__)

    def test_verification_page_returns_login_required(self) -> None:
        """验证页必须停止，不能等待超时或尝试页面内请求。"""

        collector, page, context, browser, playwright = (
            self._collector_with_browser(
                None,
                final_url="https://creator.douyin.com/verification",
            )
        )

        with self.assertRaises(DouyinDataCollectionError) as raised:
            collector.collect_browser_signed(self.account)

        self.assertEqual(raised.exception.error_code, "login_required")
        self.assertEqual((page.closed, context.closed, browser.closed), (1, 1, 1))
        self.assertEqual(playwright.stopped, 1)

    def test_cleanup_failure_overrides_apparent_metric_success(self) -> None:
        """资源未严格归零时不得写入刚捕获的指标。"""

        response = FakeBrowserResponse(
            "https://creator.douyin.com/aweme/janus/creator/data/overview/all/",
            self._current_overview_payload(),
        )
        collector, page, context, browser, playwright = (
            self._collector_with_browser(
                response,
                close_error=RuntimeError("sensitive browser path"),
            )
        )

        with self.assertRaises(DouyinDataCollectionError) as raised:
            collector.collect_browser_signed(self.account)

        self.assertEqual(
            raised.exception.error_code,
            "browser_cleanup_incomplete",
        )
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual((page.closed, context.closed, browser.closed), (1, 1, 1))
        self.assertEqual(playwright.stopped, 1)


if __name__ == "__main__":
    unittest.main()
