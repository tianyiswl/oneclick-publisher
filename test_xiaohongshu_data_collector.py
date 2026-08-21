# -*- coding: utf-8 -*-
"""小红书已审核响应的纯 payload 转换测试。"""

from __future__ import annotations

import math
import unittest

from app_core.platform_data_models import CollectionFailure, ContentRecord
from app_core.xiaohongshu_data_contract import (
    XhsContentIdentity,
    parse_account_overview,
    parse_content_lifetime,
    parse_content_list,
)


_CONTENT_ID = "6a0ffca800000000080033f8"
_OBSERVED_AT = "2026-08-21T12:00:00+08:00"
_PLATFORM_DAY = "2026-08-21"


class _IntSubclass(int):
    pass


class _StrSubclass(str):
    pass


class XiaohongshuDataContractTests(unittest.TestCase):
    def assert_invalid(self, call) -> None:
        with self.assertRaises(CollectionFailure) as raised:
            call()
        self.assertIn(
            raised.exception.error_code,
            {"metric_payload_invalid", "content_list_truncated"},
        )
        self.assertEqual(str(raised.exception), raised.exception.error_code)
        self.assertIsNone(raised.exception.__cause__)

    def test_parse_account_overview_uses_only_proven_fans_total(self) -> None:
        points = parse_account_overview(
            {"data": {"fans_count": 3199, "unknown": "ignored"}},
            observed_at=_OBSERVED_AT,
            platform_day=_PLATFORM_DAY,
        )

        self.assertEqual(
            [(point.metric_key, point.metric_value, point.metric_scope) for point in points],
            [("followers_total", 3199, "lifetime_total")],
        )
        self.assertEqual(points[0].entity_type, "account")
        self.assertEqual(points[0].entity_key, "account")
        self.assertEqual(points[0].raw_metric_key, "fans_count")
        self.assertEqual((points[0].period_start, points[0].period_end), (_PLATFORM_DAY, _PLATFORM_DAY))

    def test_account_overview_rejects_unproved_or_non_builtin_values(self) -> None:
        invalid_payloads = (
            None,
            [],
            {"data": []},
            {"data": {}},
            {"data": {"fans_count": True}},
            {"data": {"fans_count": "3199"}},
            {"data": {"fans_count": _IntSubclass(3199)}},
            {"data": {"fans_count": math.nan}},
            {"data": {"fans_count": math.inf}},
        )
        for payload in invalid_payloads:
            with self.subTest(payload_type=type(payload).__name__):
                self.assert_invalid(
                    lambda payload=payload: parse_account_overview(
                        payload,
                        observed_at=_OBSERVED_AT,
                        platform_day=_PLATFORM_DAY,
                    )
                )

    def test_content_identity_is_immutable_and_accepts_only_lowercase_hex(self) -> None:
        identity = XhsContentIdentity(_CONTENT_ID)
        self.assertEqual(identity.content_id, _CONTENT_ID)
        with self.assertRaises((AttributeError, TypeError)):
            identity.content_id = "0" * 24

        for invalid_id in ("A" * 24, "0" * 23, _StrSubclass(_CONTENT_ID), True):
            with self.subTest(invalid_id_type=type(invalid_id).__name__):
                self.assert_invalid(lambda invalid_id=invalid_id: XhsContentIdentity(invalid_id))

    def test_parse_content_list_accepts_exact_ids_and_rejects_bool_counts(self) -> None:
        rows = parse_content_list(
            {"data": {"note_infos": [{"id": _CONTENT_ID}], "total": 1}}
        )

        self.assertEqual(rows, (XhsContentIdentity(_CONTENT_ID),))
        self.assert_invalid(
            lambda: parse_content_list(
                {"data": {"note_infos": [{"id": _CONTENT_ID}], "total": True}}
            )
        )

    def test_parse_content_list_accepts_an_empty_proven_list(self) -> None:
        rows = parse_content_list({"data": {"note_infos": [], "total": 0}})

        self.assertEqual(rows, ())

    def test_content_list_rejects_unknown_containers_duplicates_and_truncation(self) -> None:
        invalid_payloads = (
            {"data": {"note_infos": {"id": _CONTENT_ID}, "total": 1}},
            {"data": {"items": [{"id": _CONTENT_ID}], "total": 1}},
            {"data": {"note_infos": [{"id": _CONTENT_ID}], "total": "1"}},
            {"data": {"note_infos": [{"id": _CONTENT_ID}, {"id": _CONTENT_ID}], "total": 2}},
            {"data": {"note_infos": [{"id": _StrSubclass(_CONTENT_ID)}], "total": 1}},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=repr(payload)):
                self.assert_invalid(lambda payload=payload: parse_content_list(payload))

        entries = [{"id": f"{index:024x}"} for index in range(51)]
        with self.assertRaises(CollectionFailure) as raised:
            parse_content_list({"data": {"note_infos": entries, "total": 51}})
        self.assertEqual(raised.exception.error_code, "content_list_truncated")
        self.assertIsNone(raised.exception.__cause__)

    def test_parse_lifetime_maps_only_reviewed_metrics_and_marks_unknown_text(self) -> None:
        content, points = parse_content_lifetime(
            {
                "data": {
                    "note_info": {
                        "id": _CONTENT_ID,
                        "view_count": 148,
                        "like_count": 5,
                        "comment_count": 9,
                        "title": "must not be used",
                    },
                    "collect_count": 2,
                    "share_count": 4,
                    "cover": "must not be used",
                }
            },
            XhsContentIdentity(_CONTENT_ID),
            observed_at=_OBSERVED_AT,
            platform_day=_PLATFORM_DAY,
        )

        self.assertEqual(content.content_id, _CONTENT_ID)
        self.assertEqual(content.title, "")
        self.assertEqual(content.cover_url, "")
        self.assertEqual(content.published_at, "")
        self.assertEqual(content.content_status, "unavailable")
        self.assertEqual(content.content_type, "unavailable")
        self.assertEqual({point.metric_key for point in points}, {"views", "likes", "comments", "favorites", "shares"})
        self.assertEqual({point.entity_key for point in points}, {_CONTENT_ID})
        self.assertTrue(all(point.metric_scope == "lifetime_total" for point in points))

    def test_lifetime_rejects_identity_mismatch_and_invalid_metric_values(self) -> None:
        base = {
            "data": {
                "note_info": {
                    "id": _CONTENT_ID,
                    "view_count": 148,
                    "like_count": 5,
                    "comment_count": 9,
                },
                "collect_count": 2,
                "share_count": 4,
            }
        }
        wrong_id = "7b0ffca800000000080033f8"
        self.assert_invalid(
            lambda: parse_content_lifetime(
                {"data": {**base["data"], "note_info": {**base["data"]["note_info"], "id": wrong_id}}},
                XhsContentIdentity(_CONTENT_ID),
                observed_at=_OBSERVED_AT,
                platform_day=_PLATFORM_DAY,
            )
        )

        for key, value in (("view_count", True), ("like_count", "5"), ("comment_count", _IntSubclass(9)), ("share_count", math.nan)):
            payload = {"data": {**base["data"], "note_info": dict(base["data"]["note_info"])}}
            if key in payload["data"]["note_info"]:
                payload["data"]["note_info"][key] = value
            else:
                payload["data"][key] = value
            with self.subTest(key=key, value_type=type(value).__name__):
                self.assert_invalid(
                    lambda payload=payload: parse_content_lifetime(
                        payload,
                        XhsContentIdentity(_CONTENT_ID),
                        observed_at=_OBSERVED_AT,
                        platform_day=_PLATFORM_DAY,
                    )
                )

    def test_lifetime_rejects_empty_or_missing_reviewed_containers(self) -> None:
        identity = XhsContentIdentity(_CONTENT_ID)
        invalid_payloads = (
            {},
            {"data": {}},
            {"data": {"note_info": []}},
            {"data": {"note_info": {"id": _CONTENT_ID}}},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=repr(payload)):
                self.assert_invalid(
                    lambda payload=payload: parse_content_lifetime(
                        payload,
                        identity,
                        observed_at=_OBSERVED_AT,
                        platform_day=_PLATFORM_DAY,
                    )
                )

    def test_content_record_allows_only_unavailable_blank_metadata(self) -> None:
        unavailable = ContentRecord(
            content_id=_CONTENT_ID,
            title="",
            cover_url="",
            published_at="",
            content_status="unavailable",
            content_type="unavailable",
        )
        self.assertEqual(unavailable.title, "")
        self.assert_invalid(
            lambda: ContentRecord(
                content_id=_CONTENT_ID,
                title="",
                cover_url="",
                published_at="",
                content_status="published",
                content_type="unavailable",
            )
        )
