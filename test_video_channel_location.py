# -*- coding: utf-8 -*-
"""视频号视频地点候选服务的离线契约测试。"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from app_core import video_channel_location_service


class VideoChannelLocationContractTest(unittest.TestCase):
    def setUp(self) -> None:
        video_channel_location_service._cache.clear()
        self.account = {
            "id": 5,
            "type": 2,
            "filePath": "video-channel.json",
        }
        self.candidate = {
            "poiId": "qqmap_11122499332116978481",
            "name": "长青公园",
            "address": "广西壮族自治区北海市海城区北京路以东,北海大道以北",
            "longitude": 109.12338256835938,
            "latitude": 21.472030639648438,
            "poiCheckSum": "980e94c1117e5231f85083983c52517f",
            "platform": "video-channel",
        }

    def test_normalize_response_keeps_official_identity_and_full_address(self) -> None:
        response = {
            "errCode": 0,
            "errMsg": "request successful",
            "data": {
                "list": [
                    {
                        "uid": "qqmap_11122499332116978481",
                        "name": "长青公园",
                        "address": "北京路以东,北海大道以北",
                        "fullAddress": "广西壮族自治区北海市海城区北京路以东,北海大道以北",
                        "longitude": 109.12338256835938,
                        "latitude": 21.472030639648438,
                        "poiCheckSum": "980e94c1117e5231f85083983c52517f",
                    }
                ]
            },
        }

        self.assertEqual(
            video_channel_location_service.normalize_location_response(response),
            [self.candidate],
        )

    def test_normalize_response_rejects_conflicting_duplicate_platform_id(self) -> None:
        response = {
            "errCode": 0,
            "data": {
                "list": [
                    {
                        "uid": "qqmap_same",
                        "name": "地点甲",
                        "fullAddress": "广西壮族自治区北海市海城区甲路1号",
                        "longitude": 109.1,
                        "latitude": 21.4,
                    },
                    {
                        "uid": "qqmap_same",
                        "name": "地点乙",
                        "fullAddress": "广西壮族自治区北海市海城区乙路2号",
                        "longitude": 109.2,
                        "latitude": 21.5,
                    },
                ]
            },
        }

        with self.assertRaisesRegex(
            video_channel_location_service.VideoChannelLocationError,
            "重复地点 ID",
        ):
            video_channel_location_service.normalize_location_response(response)

    def test_selection_is_bound_to_account_platform_scope_content_and_keyword(self) -> None:
        payload = {
            "type": 2,
            "contentType": "video",
            "accountIds": [5],
            "videoChannelLocationKeyword": "长青公园",
            "videoChannelLocationScope": "platform-default",
            "videoChannelLocationPoi": {
                **self.candidate,
                "sourceAccountId": 5,
                "platformType": 2,
                "scope": "platform-default",
                "contentType": "video",
                "searchKeyword": "长青公园",
            },
        }

        self.assertEqual(
            video_channel_location_service.normalize_location_selection(
                payload,
                expected_account_id=5,
            ),
            payload["videoChannelLocationPoi"],
        )

        payload["videoChannelLocationKeyword"] = "北部湾广场"
        with self.assertRaisesRegex(
            video_channel_location_service.VideoChannelLocationError,
            "搜索词",
        ):
            video_channel_location_service.normalize_location_selection(
                payload,
                expected_account_id=5,
            )

    def test_selection_refuses_generic_or_non_video_location_fields(self) -> None:
        payload = {
            "type": 2,
            "contentType": "video",
            "accountIds": [5],
            "locationKeyword": "长青公园",
        }
        with self.assertRaisesRegex(
            video_channel_location_service.VideoChannelLocationError,
            "通用地点字段",
        ):
            video_channel_location_service.normalize_location_selection(payload)

        payload = {
            "type": 2,
            "contentType": "article",
            "accountIds": [5],
            "videoChannelLocationKeyword": "长青公园",
        }
        with self.assertRaisesRegex(
            video_channel_location_service.VideoChannelLocationError,
            "仅支持视频",
        ):
            video_channel_location_service.normalize_location_selection(payload)

    def test_search_cache_isolated_by_account_scope_and_complete_keyword(self) -> None:
        other_account = {**self.account, "id": 6}
        with patch.object(
            video_channel_location_service,
            "_search",
            new_callable=AsyncMock,
            return_value=[self.candidate],
        ) as search:
            first = video_channel_location_service.search_video_channel_locations(
                self.account,
                "长青 公园",
            )
            second = video_channel_location_service.search_video_channel_locations(
                self.account,
                "长青 公园",
            )
            third = video_channel_location_service.search_video_channel_locations(
                self.account,
                "长青公园",
            )
            fourth = video_channel_location_service.search_video_channel_locations(
                other_account,
                "长青 公园",
            )

        self.assertEqual(first, [self.candidate])
        self.assertEqual(second, [self.candidate])
        self.assertEqual(third, [self.candidate])
        self.assertEqual(fourth, [self.candidate])
        self.assertEqual(search.await_count, 3)
        self.assertEqual(
            search.await_args_list[0].args[1],
            "长青 公园",
        )
        self.assertEqual(
            search.await_args_list[1].args[1],
            "长青公园",
        )

    def test_match_requires_platform_id_name_and_full_address(self) -> None:
        candidates = [
            self.candidate,
            {**self.candidate, "address": "另一个地址"},
            {**self.candidate, "poiId": "qqmap_other"},
        ]
        self.assertEqual(
            video_channel_location_service.location_match_indexes(
                self.candidate,
                candidates,
            ),
            [0],
        )


class _FakeTextLocator:
    def __init__(self, text: str = "", *, on_click=None) -> None:
        self.text = text
        self.on_click = on_click
        self.filled = ""

    @property
    def first(self):
        return self

    async def count(self) -> int:
        return 1

    async def is_visible(self, **_kwargs) -> bool:
        return True

    async def wait_for(self, **_kwargs) -> None:
        return None

    async def click(self, **_kwargs) -> None:
        if self.on_click:
            self.on_click()

    async def fill(self, value: str, **_kwargs) -> None:
        self.filled = value

    async def inner_text(self, **_kwargs) -> str:
        return self.text


class _FakeCandidateLocator(_FakeTextLocator):
    def __init__(self, name: str, address: str, *, on_click=None) -> None:
        super().__init__(f"{name}\n{address}", on_click=on_click)
        self.name = _FakeTextLocator(name)
        self.address = _FakeTextLocator(address)

    def locator(self, selector: str):
        return self.name if selector == ".name" else self.address


class _FakeCandidateList:
    def __init__(self, rows: list[_FakeCandidateLocator]) -> None:
        self.rows = rows

    async def count(self) -> int:
        return len(self.rows)

    def nth(self, index: int) -> _FakeCandidateLocator:
        return self.rows[index]


class _FakeFrame:
    def __init__(self, candidate: dict[str, object]) -> None:
        self.trigger = _FakeTextLocator("北海市")
        self.search = _FakeTextLocator()
        self.button = _FakeTextLocator()
        self.selected = False

        def select() -> None:
            self.selected = True
            self.trigger.text = str(candidate["name"])

        self.items = _FakeCandidateList(
            [
                _FakeCandidateLocator("不显示位置", ""),
                _FakeCandidateLocator(
                    str(candidate["name"]),
                    str(candidate["address"]),
                    on_click=select,
                ),
            ]
        )

    def locator(self, selector: str):
        if selector == ".post-position-wrap":
            return self.trigger
        if selector == 'input[placeholder="搜索附近位置"]':
            return self.search
        if selector == ".weui-desktop-search__btn":
            return self.button
        if selector == ".location-item:visible":
            return self.items
        return _FakeCandidateList([])


class _FakeResponse:
    status = 201

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    async def json(self) -> dict[str, object]:
        return self.payload


class _FakeResponseInfo:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    @property
    async def value(self) -> _FakeResponse:
        return self.response


class _FakePage:
    def __init__(self, frame: _FakeFrame, response: _FakeResponse) -> None:
        self.frames = [frame]
        self.response = response

    def expect_response(self, _predicate, **_kwargs):
        return _FakeResponseInfo(self.response)

    async def wait_for_timeout(self, _milliseconds: int) -> None:
        return None


class VideoChannelLocationApplyTest(unittest.IsolatedAsyncioTestCase):
    async def test_preflight_researches_full_identity_and_reads_selected_name(self) -> None:
        candidate = {
            "poiId": "qqmap_11122499332116978481",
            "name": "长青公园",
            "address": "广西壮族自治区北海市海城区北京路以东,北海大道以北",
            "longitude": 109.12338256835938,
            "latitude": 21.472030639648438,
            "poiCheckSum": "980e94c1117e5231f85083983c52517f",
            "platform": "video-channel",
        }
        selection = {
            **candidate,
            "sourceAccountId": 5,
            "platformType": 2,
            "scope": "platform-default",
            "contentType": "video",
            "searchKeyword": "长青公园",
        }
        payload = {
            "type": 2,
            "contentType": "video",
            "accountIds": [5],
            "videoChannelLocationKeyword": "长青公园",
            "videoChannelLocationScope": "platform-default",
            "videoChannelLocationPoi": selection,
        }
        response = _FakeResponse(
            {
                "errCode": 0,
                "data": {
                    "list": [
                        {
                            "uid": candidate["poiId"],
                            "name": candidate["name"],
                            "fullAddress": candidate["address"],
                            "longitude": candidate["longitude"],
                            "latitude": candidate["latitude"],
                            "poiCheckSum": candidate["poiCheckSum"],
                        }
                    ]
                },
            }
        )
        frame = _FakeFrame(candidate)

        result = await video_channel_location_service.apply_video_channel_location(
            _FakePage(frame, response),
            payload,
            expected_account_id=5,
        )

        self.assertEqual(result, selection)
        self.assertTrue(frame.selected)
        self.assertEqual(frame.search.filled, "长青公园")



if __name__ == "__main__":
    unittest.main()
