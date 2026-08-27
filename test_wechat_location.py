# -*- coding: utf-8 -*-
"""公众号正文地理位置的离线安全契约测试。"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from app_core import wechat_location_service


class WechatLocationContractTests(unittest.TestCase):
    def setUp(self) -> None:
        wechat_location_service._cache.clear()

    @staticmethod
    def _official_candidate(**overrides: object) -> dict[str, object]:
        value: dict[str, object] = {
            "poiid": "12451333552992790600",
            "name": "北海银滩国家旅游度假区",
            "address": "广西壮族自治区北海市银海区潮街",
            "latitude": "21.405443",
            "longitude": "109.148628",
        }
        value.update(overrides)
        return value

    def test_official_response_keeps_complete_wechat_poi_identity(self) -> None:
        response = {
            "base_resp": {"ret": 0},
            "data": {"poi_list": [self._official_candidate()]},
        }

        self.assertEqual(
            wechat_location_service.normalize_location_response(response),
            [
                {
                    "poiId": "12451333552992790600",
                    "name": "北海银滩国家旅游度假区",
                    "address": "广西壮族自治区北海市银海区潮街",
                    "latitude": "21.405443",
                    "longitude": "109.148628",
                    "platform": "wechat_article",
                }
            ],
        )

    def test_incomplete_candidate_is_never_exposed(self) -> None:
        rows = [
            self._official_candidate(),
            self._official_candidate(poiid="missing-address", address=""),
            self._official_candidate(poiid="missing-latitude", latitude=""),
            self._official_candidate(poiid="missing-longitude", longitude=""),
        ]
        result = wechat_location_service.normalize_location_response({"list": rows})
        self.assertEqual(len(result), 1)

    def test_conflicting_duplicate_poi_stops_instead_of_guessing(self) -> None:
        with self.assertRaisesRegex(
            wechat_location_service.WechatLocationError,
            "重复 POI",
        ):
            wechat_location_service.normalize_location_response(
                {
                    "list": [
                        self._official_candidate(),
                        self._official_candidate(name="另一个地点名"),
                    ]
                }
            )

    def test_selection_is_bound_to_account_scope_and_full_keyword(self) -> None:
        payload = {
            "type": 10,
            "contentType": "article",
            "accountIds": [27],
            "wechatLocationKeyword": "北海 银滩景区",
            "wechatLocationScope": "article-inline-poi",
            "wechatLocationPoi": {
                **wechat_location_service.normalize_official_location_candidate(
                    self._official_candidate()
                ),
                "sourceAccountId": 27,
                "platformType": 10,
                "scope": "article-inline-poi",
                "contentType": "article",
                "searchKeyword": "北海 银滩景区",
            },
        }
        selected = wechat_location_service.normalize_location_selection(payload)
        self.assertEqual(selected["sourceAccountId"], 27)
        self.assertEqual(selected["searchKeyword"], "北海 银滩景区")

        for field, value in (
            ("accountIds", [28]),
            ("wechatLocationKeyword", "北海银滩"),
            ("wechatLocationScope", "platform-default"),
            ("contentType", "video"),
        ):
            unsafe = dict(payload)
            unsafe[field] = value
            with self.subTest(field=field), self.assertRaises(
                wechat_location_service.WechatLocationError
            ):
                wechat_location_service.normalize_location_selection(unsafe)

    def test_generic_location_fields_are_rejected(self) -> None:
        payload = {
            "type": 10,
            "contentType": "article",
            "accountIds": [27],
            "locationKeyword": "北海银滩",
        }
        with self.assertRaisesRegex(
            wechat_location_service.WechatLocationError,
            "通用地点字段",
        ):
            wechat_location_service.normalize_location_selection(payload)

    def test_cache_isolated_by_account_and_full_keyword(self) -> None:
        first = self._official_candidate()
        second = self._official_candidate(
            poiid="poi-2",
            name="北海老街",
            address="广西壮族自治区北海市海城区珠海路",
        )
        account_a = {"id": 27, "type": 10, "filePath": "wechat-a.json"}
        account_b = {"id": 28, "type": 10, "filePath": "wechat-b.json"}
        with patch.object(
            wechat_location_service,
            "_search",
            new=AsyncMock(
                side_effect=[
                    [wechat_location_service.normalize_official_location_candidate(first)],
                    [wechat_location_service.normalize_official_location_candidate(second)],
                    [wechat_location_service.normalize_official_location_candidate(first)],
                ]
            ),
        ) as search:
            one = wechat_location_service.search_wechat_locations(account_a, "北海 银滩")
            cached = wechat_location_service.search_wechat_locations(account_a, "北海 银滩")
            other_keyword = wechat_location_service.search_wechat_locations(account_a, "北海老街")
            other_account = wechat_location_service.search_wechat_locations(account_b, "北海 银滩")

        self.assertEqual(one, cached)
        self.assertEqual(other_keyword[0]["poiId"], "poi-2")
        self.assertEqual(other_account[0]["poiId"], first["poiid"])
        self.assertEqual(search.await_count, 3)

if __name__ == "__main__":
    unittest.main()
