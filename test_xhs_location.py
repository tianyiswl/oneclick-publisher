# -*- coding: utf-8 -*-
"""小红书视频地点候选服务的离线契约测试。"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from app_core import xhs_location_service


class XhsLocationCandidateTests(unittest.TestCase):
    def test_candidate_accepts_poi_id_or_new_poi_id_and_keeps_zero_type(self) -> None:
        self.assertEqual(
            xhs_location_service.normalize_location_candidate(
                {
                    "newPoiId": "poi-1",
                    "name": "北海银滩",
                    "fullAddress": "广西北海市银海区银滩大道",
                    "poiType": 0,
                }
            ),
            {
                "poiId": "poi-1",
                "name": "北海银滩",
                "address": "广西北海市银海区银滩大道",
                "poiType": "0",
                "platform": "xiaohongshu",
            },
        )

    def test_candidate_requires_complete_platform_identity(self) -> None:
        complete = {
            "poiId": "poi-1",
            "name": "北海银滩",
            "fullAddress": "广西北海市银海区银滩大道",
            "poiType": "spot",
        }
        self.assertIsNotNone(xhs_location_service.normalize_location_candidate(complete))
        for field in ("poiId", "name", "fullAddress"):
            with self.subTest(field=field):
                incomplete = dict(complete)
                incomplete.pop(field)
                self.assertIsNone(
                    xhs_location_service.normalize_location_candidate(incomplete)
                )

    def test_candidate_keeps_missing_poi_type_as_safe_empty_string(self) -> None:
        self.assertEqual(
            xhs_location_service.normalize_location_candidate(
                {
                    "poiId": "poi-1",
                    "name": "北海银滩",
                    "fullAddress": "广西北海市银海区银滩大道",
                }
            ),
            {
                "poiId": "poi-1",
                "name": "北海银滩",
                "address": "广西北海市银海区银滩大道",
                "poiType": "",
                "platform": "xiaohongshu",
            },
        )

    def test_response_scans_every_row_before_applying_ui_limit(self) -> None:
        addressless = [
            {"poiId": f"missing-{index}", "name": f"缺地址 {index}", "poiType": 0}
            for index in range(xhs_location_service.MAX_RESULTS)
        ]
        complete = {
            "poiId": "complete",
            "name": "北海银滩",
            "fullAddress": "广西北海市银海区银滩大道",
            "poiType": 0,
        }
        response = {"data": {"poiList": [*addressless, complete]}}

        self.assertEqual(
            xhs_location_service.normalize_location_response(response),
            [
                {
                    "poiId": "complete",
                    "name": "北海银滩",
                    "address": "广西北海市银海区银滩大道",
                    "poiType": "0",
                    "platform": "xiaohongshu",
                }
            ],
        )

    def test_response_rejects_same_poi_id_with_different_identity(self) -> None:
        first = {
            "poiId": "same",
            "name": "北海银滩",
            "fullAddress": "北海市银海区",
            "poiType": 0,
        }
        for changed in (
            {**first, "name": "北海银滩景区"},
            {**first, "fullAddress": "北海市海城区"},
        ):
            with self.subTest(changed=changed):
                with self.assertRaisesRegex(
                    xhs_location_service.XhsLocationSearchError, "重复 POI"
                ):
                    xhs_location_service.normalize_location_response(
                        {"poiList": [first, changed]}
                    )

    def test_response_rejects_non_list_poi_list(self) -> None:
        with self.assertRaisesRegex(
            xhs_location_service.XhsLocationSearchError, "无效数据"
        ):
            xhs_location_service.normalize_location_response({"poiList": {}})

    def test_match_requires_complete_three_field_identity(self) -> None:
        target = {
            "poiId": "poi-1",
            "name": "同名地点",
            "address": "北海市银海区",
            "poiType": "0",
            "platform": "xiaohongshu",
        }
        candidates = [
            {**target, "address": "北海市海城区"},
            target,
        ]
        self.assertEqual(xhs_location_service.location_match_indexes(target, candidates), [1])

    def test_match_uses_only_the_three_required_identity_fields(self) -> None:
        target = {"poiId": "poi-1", "name": "北海银滩", "address": "北海市银海区"}
        self.assertEqual(
            xhs_location_service.location_match_indexes(
                target,
                [
                    {**target, "address": "北海市海城区"},
                    target,
                ],
            ),
            [1],
        )

    def test_default_limit_does_not_hide_full_revalidation_rows(self) -> None:
        response = {
            "poiList": [
                {
                    "poiId": f"poi-{index}",
                    "name": f"地点 {index}",
                    "fullAddress": f"北海市地址 {index}",
                    "poiType": 0,
                }
                for index in range(xhs_location_service.MAX_RESULTS + 1)
            ]
        }
        self.assertEqual(
            len(xhs_location_service.normalize_location_response(response)),
            xhs_location_service.MAX_RESULTS,
        )
        self.assertEqual(
            len(xhs_location_service.normalize_location_response(response, limit=None)),
            xhs_location_service.MAX_RESULTS + 1,
        )


class XhsLocationSelectionAndCacheTests(unittest.TestCase):
    account = {"id": 123, "type": 1, "filePath": "xhs-123.json"}
    rows = [
        {
            "poiId": "poi-1",
            "name": "北海银滩",
            "address": "广西北海市银海区银滩大道",
            "poiType": "0",
            "platform": "xiaohongshu",
        }
    ]

    def setUp(self) -> None:
        xhs_location_service._cache.clear()

    def _payload(self, **changes: object) -> dict[str, object]:
        selection = {
            **self.rows[0],
            "sourceAccountId": 123,
            "platformType": 1,
            "scope": "platform-default",
            "contentType": "video",
            "searchKeyword": "北海银滩",
        }
        payload: dict[str, object] = {
            "type": 1,
            "contentType": "video",
            "accountIds": [123],
            "xhsLocationKeyword": "北海银滩",
            "xhsLocationScope": "platform-default",
            "xhsLocationPoi": selection,
        }
        payload.update(changes)
        return payload

    def test_selection_returns_full_canonical_identity(self) -> None:
        self.assertEqual(
            xhs_location_service.normalize_location_selection(self._payload()),
            {
                **self.rows[0],
                "sourceAccountId": 123,
                "platformType": 1,
                "scope": "platform-default",
                "contentType": "video",
                "searchKeyword": "北海银滩",
            },
        )

    def test_selection_rejects_incomplete_or_cross_context_payloads(self) -> None:
        cases = [
            self._payload(xhsLocationPoi=None),
            self._payload(contentType="image"),
            self._payload(xhsLocationScope="domestic"),
            self._payload(type=3),
            self._payload(accountIds=[999]),
            self._payload(locationKeyword="北海银滩"),
            self._payload(locationPoi={"poiId": "other"}),
            self._payload(locationScope="domestic"),
            self._payload(
                xhsLocationPoi={
                    **self._payload()["xhsLocationPoi"],  # type: ignore[arg-type]
                    "platform": "douyin",
                }
            ),
        ]
        mismatched_keyword = self._payload()
        mismatched_keyword["xhsLocationPoi"] = {
            **mismatched_keyword["xhsLocationPoi"],  # type: ignore[arg-type]
            "searchKeyword": "北海老街",
        }
        cases.append(mismatched_keyword)
        for index, payload in enumerate(cases):
            with self.subTest(index=index):
                with self.assertRaises(xhs_location_service.XhsLocationSearchError):
                    xhs_location_service.normalize_location_selection(payload)
        with self.assertRaises(xhs_location_service.XhsLocationSearchError):
            xhs_location_service.normalize_location_selection(
                self._payload(), expected_account_id=999
            )

    def test_selection_requires_the_complete_saved_selection_context(self) -> None:
        for field in ("platform", "platformType", "scope", "contentType", "searchKeyword"):
            payload = self._payload()
            selection = dict(payload["xhsLocationPoi"])  # type: ignore[arg-type]
            selection.pop(field)
            payload["xhsLocationPoi"] = selection
            with self.subTest(field=field):
                with self.assertRaises(xhs_location_service.XhsLocationSearchError):
                    xhs_location_service.normalize_location_selection(payload)

    def test_no_location_selection_returns_none(self) -> None:
        payload = self._payload(
            xhsLocationKeyword="", xhsLocationScope="", xhsLocationPoi=None
        )
        self.assertIsNone(xhs_location_service.normalize_location_selection(payload))

    def test_cache_is_five_dimensional_and_expires_at_exactly_sixty_seconds(self) -> None:
        other_account = {"id": 456, "type": 1, "filePath": "xhs-456.json"}
        with patch.object(
            xhs_location_service,
            "_search",
            new_callable=AsyncMock,
            return_value=self.rows,
        ) as search:
            self.assertEqual(
                xhs_location_service.search_xhs_locations(self.account, "北海银滩"),
                self.rows,
            )
            self.assertEqual(
                xhs_location_service.search_xhs_locations(self.account, "北海银滩"),
                self.rows,
            )
            self.assertEqual(
                xhs_location_service.search_xhs_locations(other_account, "北海银滩"),
                self.rows,
            )
            self.assertEqual(
                xhs_location_service.search_xhs_locations(self.account, "北海老街"),
                self.rows,
            )
            key = (123, 1, "platform-default", "video", "北海银滩")
            stored_at, cached_rows = xhs_location_service._cache[key]
            self.assertIsInstance(stored_at, float)
            xhs_location_service._cache[key] = (
                xhs_location_service.time.monotonic() - 60.0,
                cached_rows,
            )
            self.assertEqual(
                xhs_location_service.search_xhs_locations(self.account, "北海银滩"),
                self.rows,
            )

        self.assertEqual(search.await_count, 4)
        with self.assertRaises(xhs_location_service.XhsLocationSearchError):
            xhs_location_service.search_xhs_locations(
                {**self.account, "type": 2}, "北海银滩"
            )
        with self.assertRaises(xhs_location_service.XhsLocationSearchError):
            xhs_location_service.search_xhs_locations(
                self.account, "北海银滩", scope="domestic"
            )
        with self.assertRaises(xhs_location_service.XhsLocationSearchError):
            xhs_location_service.search_xhs_locations(
                self.account, "北海银滩", content_type="image"
            )

    def test_search_returns_the_ui_candidate_cap_after_full_response_validation(self) -> None:
        complete_rows = [
            {
                "poiId": f"poi-{index}",
                "name": f"地点 {index}",
                "address": f"北海市地址 {index}",
                "poiType": "0",
                "platform": "xiaohongshu",
            }
            for index in range(xhs_location_service.MAX_RESULTS + 1)
        ]
        with patch.object(
            xhs_location_service, "_search", new_callable=AsyncMock, return_value=complete_rows
        ):
            result = xhs_location_service.search_xhs_locations(self.account, "北海银滩")
        self.assertEqual(result, complete_rows[: xhs_location_service.MAX_RESULTS])


if __name__ == "__main__":
    unittest.main()
