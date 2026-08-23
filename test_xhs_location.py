# -*- coding: utf-8 -*-
"""小红书视频地点候选服务的离线契约测试。"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel

from app_core import (
    account_service,
    oneclick_preflight,
    publish_service,
    xhs_location_service,
)
from app_core.xhs_native_adapter import (
    XhsNativeAdapter,
    XhsNativeAdapterError,
    build_native_contract,
)
from app_core.xhs_publish_executor import XhsPublishError, run_xhs_publish
from ui.publish_page import PublishPage


_MALFORMED_POST_DATA = object()
_XHS_CREATOR_PUBLISH_URL = (
    "https://creator.xiaohongshu.com/publish/publish?source=official"
)


class XhsLocationUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_location_panel_is_visible_only_for_one_total_xhs_video_account(self) -> None:
        page = PublishPage()
        xhs = {"id": 11, "type": 1, "filePath": "xhs-11.json"}
        second_xhs = {"id": 12, "type": 1, "filePath": "xhs-12.json"}
        douyin = {"id": 31, "type": 3, "filePath": "douyin-31.json"}
        try:
            self.assertIsNot(page.xhs_location_keyword, page.douyin_location_keyword)
            cases = [
                ("video", [], True),
                ("video", [xhs], False),
                ("video", [xhs, second_xhs], True),
                ("video", [xhs, douyin], True),
                ("video", [douyin], True),
                ("article", [xhs], True),
            ]
            for content_type, accounts, expected_hidden in cases:
                with self.subTest(content_type=content_type, accounts=accounts):
                    page.content_type = content_type
                    with patch.object(page, "selected_accounts", return_value=accounts):
                        page._sync_xhs_location_visibility_and_context()
                    self.assertEqual(
                        page.xhs_location_panel.isHidden(),
                        expected_hidden,
                    )
        finally:
            page.close()

    def test_account_or_content_change_invalidates_selection_and_candidates(self) -> None:
        page = PublishPage()
        xhs = {"id": 11, "type": 1, "filePath": "xhs-11.json"}
        douyin = {"id": 31, "type": 3, "filePath": "douyin-31.json"}
        try:
            page.content_type = "video"
            with patch.object(page, "selected_accounts", return_value=[xhs]):
                page.update_selected_labels()
            page._xhs_selected_location = {"poiId": "poi-old"}
            page.xhs_location_results.addItem("旧候选")
            page.xhs_location_results.setVisible(True)

            with patch.object(page, "selected_accounts", return_value=[xhs, douyin]):
                page.update_selected_labels()

            self.assertTrue(page.xhs_location_panel.isHidden())
            self.assertEqual(page._xhs_selected_location, {})
            self.assertEqual(page.xhs_location_results.count(), 0)

            page._xhs_selected_location = {"poiId": "poi-old-again"}
            page.xhs_location_results.addItem("另一个旧候选")
            page.content_type = "article"
            with patch.object(page, "selected_accounts", return_value=[xhs]):
                page.update_selected_labels()
            self.assertEqual(page._xhs_selected_location, {})
            self.assertEqual(page.xhs_location_results.count(), 0)
        finally:
            page.close()

    def test_editing_keyword_invalidates_selection_and_candidates(self) -> None:
        page = PublishPage()
        xhs = {"id": 11, "type": 1, "filePath": "xhs-11.json"}
        try:
            with patch.object(page, "selected_accounts", return_value=[xhs]):
                page.update_selected_labels()
            page._xhs_selected_location = {"poiId": "poi-old"}
            page.xhs_location_results.addItem("旧候选")
            page.xhs_location_results.setVisible(True)
            page.xhs_location_keyword.setText("北海老街")

            page.xhs_location_keyword.textEdited.emit("北海老街")

            self.assertEqual(page._xhs_selected_location, {})
            self.assertEqual(page.xhs_location_results.count(), 0)
        finally:
            page.close()

    def test_candidate_card_and_selection_keep_full_query_and_context(self) -> None:
        page = PublishPage()
        xhs = {"id": 11, "type": 1, "filePath": "xhs-11.json"}
        try:
            with patch.object(page, "selected_accounts", return_value=[xhs]):
                page.update_selected_labels()
            page.xhs_location_keyword.setText("  北海  银滩  ")
            page._show_xhs_location_results(
                [
                    {
                        "poiId": "poi-1",
                        "name": "北海银滩景区",
                        "address": "广西北海市银海区银滩大道",
                        "poiType": "0",
                        "platform": "xiaohongshu",
                    }
                ],
                source_account_id=11,
                search_keyword="北海 银滩",
                content_type="video",
            )

            item = page.xhs_location_results.item(0)
            card = page.xhs_location_results.itemWidget(item)
            self.assertEqual(
                card.findChild(QLabel, "xhsLocationName").text(),
                "北海银滩景区",
            )
            self.assertEqual(
                card.findChild(QLabel, "xhsLocationAddress").text(),
                "广西北海市银海区银滩大道",
            )
            self.assertEqual(
                card.findChild(QLabel, "xhsLocationPoi").text(),
                "小红书 POI · poi-1",
            )

            with patch.object(page, "selected_accounts", return_value=[xhs]):
                page._select_xhs_location_item(item)

            self.assertEqual(page.xhs_location_keyword.text(), "北海 银滩")
            self.assertEqual(
                page._xhs_selected_location,
                {
                    "poiId": "poi-1",
                    "name": "北海银滩景区",
                    "address": "广西北海市银海区银滩大道",
                    "poiType": "0",
                    "platform": "xiaohongshu",
                    "sourceAccountId": 11,
                    "platformType": 1,
                    "scope": "platform-default",
                    "contentType": "video",
                    "searchKeyword": "北海 银滩",
                },
            )
        finally:
            page.close()

    def test_search_uses_dedicated_async_key_and_exact_service_context(self) -> None:
        page = PublishPage()
        xhs = {"id": 11, "type": 1, "filePath": "xhs-11.json"}
        try:
            page.xhs_location_keyword.setText("  北海  银滩  ")
            with patch.object(page, "selected_accounts", return_value=[xhs]), patch.object(
                page.location_tasks,
                "run",
                return_value=True,
            ) as run:
                page.search_xhs_locations()

            self.assertEqual(run.call_args.args[0], "xhs-location-search")
            work = run.call_args.args[1]
            with patch.object(
                xhs_location_service,
                "search_xhs_locations",
                return_value=[],
            ) as search:
                work()
            search.assert_called_once_with(
                xhs,
                "北海 银滩",
                "platform-default",
                "video",
            )
        finally:
            page.close()

    def test_running_search_dispatches_latest_pending_keyword_after_cleanup(self) -> None:
        page = PublishPage()
        xhs = {"id": 11, "type": 1, "filePath": "xhs-11.json"}
        calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        runner_state = {"active": False}

        def is_running(key: str) -> bool:
            self.assertEqual(key, "xhs-location-search")
            return runner_state["active"]

        def run(*args: object, **kwargs: object) -> bool:
            if runner_state["active"]:
                return False
            runner_state["active"] = True
            calls.append((args, kwargs))
            return True

        try:
            with patch.object(page, "selected_accounts", return_value=[xhs]), patch.object(
                page.location_tasks,
                "is_running",
                side_effect=is_running,
            ), patch.object(page.location_tasks, "run", side_effect=run):
                page.xhs_location_keyword.setText("地点 A")
                page.search_xhs_locations()
                self.assertEqual(len(calls), 1)

                page.xhs_location_keyword.setText("地点 B")
                page.xhs_location_keyword.textEdited.emit("地点 B")
                page.search_xhs_locations()
                page.search_xhs_locations()
                self.assertEqual(len(calls), 1)

                runner_state["active"] = False
                calls[0][1]["on_finished"]()
                self.assertEqual(len(calls), 2)

            with patch.object(
                xhs_location_service,
                "search_xhs_locations",
                return_value=[],
            ) as search:
                calls[1][0][1]()
            search.assert_called_once_with(
                xhs,
                "地点 B",
                "platform-default",
                "video",
            )
        finally:
            page.close()

    def test_stale_success_is_rejected_for_total_account_or_generation_change(self) -> None:
        page = PublishPage()
        xhs = {"id": 11, "type": 1, "filePath": "xhs-11.json"}
        douyin = {"id": 31, "type": 3, "filePath": "douyin-31.json"}
        row = {
            "poiId": "poi-1",
            "name": "北海银滩景区",
            "address": "广西北海市银海区银滩大道",
            "poiType": "0",
            "platform": "xiaohongshu",
        }
        try:
            page.xhs_location_keyword.setText("北海银滩")
            calls = []

            def capture(*args, **kwargs) -> bool:
                calls.append((args, kwargs))
                return True

            with patch.object(page, "selected_accounts", return_value=[xhs]), patch.object(
                page.location_tasks,
                "run",
                side_effect=capture,
            ):
                page.search_xhs_locations()
            first_success = calls[-1][1]["on_success"]

            with patch.object(
                page,
                "selected_accounts",
                return_value=[xhs, douyin],
            ):
                first_success([row])
            self.assertEqual(page.xhs_location_results.count(), 0)

            calls.clear()
            with patch.object(page, "selected_accounts", return_value=[xhs]), patch.object(
                page.location_tasks,
                "run",
                side_effect=capture,
            ):
                page.search_xhs_locations()
            stale_a_success = calls[-1][1]["on_success"]
            page.xhs_location_keyword.setText("北海老街")
            page.xhs_location_keyword.textEdited.emit("北海老街")
            page.xhs_location_keyword.setText("北海银滩")
            page.xhs_location_keyword.textEdited.emit("北海银滩")
            with patch.object(page, "selected_accounts", return_value=[xhs]):
                stale_a_success([row])
            self.assertEqual(page.xhs_location_results.count(), 0)
        finally:
            page.close()


class XhsLocationPayloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _account(account_id: int = 11) -> dict[str, object]:
        return {
            "id": account_id,
            "type": 1,
            "platformName": "小红书",
            "filePath": f"xhs-{account_id}.json",
        }

    @staticmethod
    def _selection(account_id: int = 11) -> dict[str, object]:
        return {
            "poiId": "poi-1",
            "name": "北海银滩景区",
            "address": "广西北海市银海区银滩大道",
            "poiType": "0",
            "platform": "xiaohongshu",
            "sourceAccountId": account_id,
            "platformType": 1,
            "scope": "platform-default",
            "contentType": "video",
            "searchKeyword": "北海 银滩",
        }

    def _collect(self, page: PublishPage, accounts: list[dict]) -> list[dict]:
        media = {"id": 8, "file_path": "offline.mp4"}
        with patch.object(page, "selected_accounts", return_value=accounts), patch.object(
            page,
            "selected_media",
            return_value=[media],
        ), patch("ui.publish_page.publish_config_service.save_tags"):
            return page.collect_payloads("preflight")

    def test_blank_or_selected_location_uses_only_xhs_fields(self) -> None:
        page = PublishPage()
        account = self._account()
        try:
            page.common_title_input.setText("定位功能测试")
            page.title_input.setPlainText("只做离线载荷测试")

            blank = self._collect(page, [account])[0]
            self.assertEqual(blank["xhsLocationKeyword"], "")
            self.assertEqual(blank["xhsLocationScope"], "")
            self.assertIsNone(blank["xhsLocationPoi"])
            for generic_key in ("locationKeyword", "locationScope", "locationPoi"):
                self.assertNotIn(generic_key, blank)

            page.xhs_location_keyword.setText("北海 银滩")
            page._xhs_selected_location = self._selection()
            selected = self._collect(page, [account])[0]
            self.assertEqual(selected["xhsLocationKeyword"], "北海 银滩")
            self.assertEqual(selected["xhsLocationScope"], "platform-default")
            self.assertEqual(selected["xhsLocationPoi"], self._selection())
            for generic_key in ("locationKeyword", "locationScope", "locationPoi"):
                self.assertNotIn(generic_key, selected)
        finally:
            page.close()

    def test_keyword_without_selected_candidate_fails_collect_payloads(self) -> None:
        page = PublishPage()
        try:
            page.common_title_input.setText("定位功能测试")
            page.title_input.setPlainText("只做离线载荷测试")
            page.xhs_location_keyword.setText("北海银滩")
            with self.assertRaisesRegex(ValueError, "不能只填写关键词"):
                self._collect(page, [self._account()])
        finally:
            page.close()

    def test_templates_neither_persist_nor_restore_xhs_selection(self) -> None:
        page = PublishPage()
        try:
            page.xhs_location_keyword.setText("北海 银滩")
            page._xhs_selected_location = self._selection()
            page.xhs_location_results.addItem("旧候选")

            template = page.payload_for_template()
            saved_content = page.payload_for_saved_content()["content"]
            for key in ("xhsLocationKeyword", "xhsLocationScope", "xhsLocationPoi"):
                self.assertNotIn(key, template)
                self.assertNotIn(key, saved_content)

            page._apply_content_payload(
                {
                    "xhsLocationKeyword": "北海 银滩",
                    "xhsLocationScope": "platform-default",
                    "xhsLocationPoi": self._selection(account_id=999),
                }
            )
            self.assertEqual(page.xhs_location_keyword.text(), "")
            self.assertEqual(page._xhs_selected_location, {})
            self.assertEqual(page.xhs_location_results.count(), 0)
        finally:
            page.close()

    def _service_payload(self, media_path: Path, **changes: object) -> dict[str, object]:
        selection = self._selection()
        payload: dict[str, object] = {
            "type": 1,
            "contentType": "video",
            "runtimeMode": "preflight",
            "debugDryRun": True,
            "title": "定位服务测试",
            "description": "只验证浏览器启动前的载荷边界。",
            "fileList": [str(media_path)],
            "accountList": ["xhs-11.json"],
            "accountIds": [11],
            "xhsLocationKeyword": "北海 银滩",
            "xhsLocationScope": "platform-default",
            "xhsLocationPoi": selection,
        }
        payload.update(changes)
        return payload

    def test_publish_service_normalizes_video_location_and_keeps_plain_article(self) -> None:
        with TemporaryDirectory() as directory:
            video = Path(directory) / "fixture.mp4"
            video.write_bytes(b"offline-video")
            image = Path(directory) / "fixture.png"
            image.write_bytes(b"offline-image")
            selection = {
                **self._selection(),
                "name": "  北海银滩景区 ",
                "address": " 广西北海市银海区银滩大道 ",
                "searchKeyword": "  北海   银滩 ",
            }
            normalized = publish_service._validate_payloads(
                [
                    self._service_payload(
                        video,
                        xhsLocationKeyword="  北海   银滩 ",
                        xhsLocationPoi=selection,
                        locationKeyword="",
                        locationScope="",
                        locationPoi={},
                    )
                ]
            )[0]
            self.assertEqual(normalized["xhsLocationKeyword"], "北海 银滩")
            self.assertEqual(normalized["xhsLocationScope"], "platform-default")
            self.assertEqual(
                normalized["xhsLocationPoi"],
                {
                    **self._selection(),
                    "name": "北海银滩景区",
                    "address": "广西北海市银海区银滩大道",
                },
            )
            for generic_key in ("locationKeyword", "locationScope", "locationPoi"):
                self.assertNotIn(generic_key, normalized)

            article = self._service_payload(
                image,
                contentType="article",
            )
            for key in ("xhsLocationKeyword", "xhsLocationScope", "xhsLocationPoi"):
                article.pop(key)
            self.assertEqual(
                publish_service._validate_payloads([article])[0]["contentType"],
                "article",
            )

    def test_publish_service_rejects_cross_context_location_before_task_creation(self) -> None:
        with TemporaryDirectory() as directory:
            video = Path(directory) / "fixture.mp4"
            video.write_bytes(b"offline-video")
            image = Path(directory) / "fixture.png"
            image.write_bytes(b"offline-image")
            base = self._service_payload(video)
            cases = [
                {
                    **base,
                    "xhsLocationPoi": None,
                },
                {
                    **base,
                    "xhsLocationScope": "domestic",
                },
                {
                    **base,
                    "accountIds": [99],
                },
                {
                    **base,
                    "contentType": "article",
                    "fileList": [str(image)],
                },
                {
                    **base,
                    "contentType": "article",
                    "fileList": [str(image)],
                    "xhsLocationKeyword": "",
                    "xhsLocationScope": "",
                    "xhsLocationPoi": None,
                },
                {
                    **base,
                    "contentType": "article",
                    "fileList": [str(image)],
                    "locationKeyword": "北海银滩",
                },
                {
                    **base,
                    "locationKeyword": "北海银滩",
                },
            ]
            for index, payload in enumerate(cases):
                with self.subTest(index=index):
                    with self.assertRaises(ValueError):
                        publish_service._validate_payloads([payload])

            keyword_only = {**base, "xhsLocationPoi": None}
            with patch.object(
                publish_service.task_service,
                "create_pending_task",
            ) as create_task:
                with self.assertRaises(ValueError):
                    publish_service.start_desktop_publish([keyword_only])
            create_task.assert_not_called()


class _SearchBoundaryPage:
    def __init__(self, *, current_url: str) -> None:
        self.url = current_url
        self.goto_calls: list[tuple[str, dict[str, object]]] = []
        self.evaluate_calls: list[tuple[str, object]] = []
        self.fetch_calls: list[dict[str, object]] = []

    async def goto(self, url: str, **kwargs: object) -> None:
        self.goto_calls.append((url, dict(kwargs)))

    async def evaluate(self, script: str, payload: object) -> object:
        self.evaluate_calls.append((script, payload))
        request = payload if isinstance(payload, dict) else {}
        self.fetch_calls.append(
            {
                "endpoint": request.get("endpoint"),
                "method": request.get("method"),
                "credentials": request.get("credentials"),
                "headers": request.get("headers"),
                "body": request.get("body"),
            }
        )
        return {
            "httpStatus": 200,
            "data": {
                "poiList": [
                    {
                        "poiId": "poi-1",
                        "name": "北海银滩景区",
                        "fullAddress": "广西北海市银海区银滩大道",
                        "poiType": 0,
                    }
                ]
            },
        }


class _SearchBoundaryContext:
    def __init__(self, page: _SearchBoundaryPage) -> None:
        self.page = page
        self.close_calls = 0
        self.close_error: Exception | None = None

    async def new_page(self) -> _SearchBoundaryPage:
        return self.page

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class _SearchBoundaryBrowser:
    def __init__(self, page: _SearchBoundaryPage) -> None:
        self.context = _SearchBoundaryContext(page)
        self.context_kwargs: list[dict[str, object]] = []
        self.close_calls = 0

    async def new_context(self, **kwargs: object) -> _SearchBoundaryContext:
        self.context_kwargs.append(dict(kwargs))
        return self.context

    async def close(self) -> None:
        self.close_calls += 1


class _SearchBoundaryChromium:
    def __init__(self, browser: _SearchBoundaryBrowser) -> None:
        self.browser = browser
        self.launch_kwargs: list[dict[str, object]] = []

    async def launch(self, **kwargs: object) -> _SearchBoundaryBrowser:
        self.launch_kwargs.append(dict(kwargs))
        return self.browser


class _SearchBoundaryPlaywrightManager:
    def __init__(self, page: _SearchBoundaryPage) -> None:
        self.browser = _SearchBoundaryBrowser(page)
        self.chromium = _SearchBoundaryChromium(self.browser)
        self.exit_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self.exit_calls += 1


class XhsLocationSearchBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_uses_creator_page_and_exact_official_post_protocol(self) -> None:
        state = Path("/offline/xhs-11.json")
        page = _SearchBoundaryPage(current_url=_XHS_CREATOR_PUBLISH_URL)
        manager = _SearchBoundaryPlaywrightManager(page)
        from playwright import async_api

        with patch.object(
            xhs_location_service,
            "_storage_state",
            return_value=state,
        ), patch.object(async_api, "async_playwright", return_value=manager):
            result = await xhs_location_service._search(
                {"id": 11, "type": 1, "filePath": "xhs-11.json"},
                "北海 银滩",
            )

        self.assertEqual(
            page.goto_calls,
            [
                (
                    _XHS_CREATOR_PUBLISH_URL,
                    {"wait_until": "domcontentloaded", "timeout": 45_000},
                )
            ],
        )
        self.assertEqual(
            page.fetch_calls,
            [
                {
                    "endpoint": (
                        "https://edith.xiaohongshu.com/"
                        "web_api/sns/v1/local/poi/creator/search"
                    ),
                    "method": "POST",
                    "credentials": "include",
                    "headers": {
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    "body": {
                        "latitude": 0,
                        "longitude": 0,
                        "keyword": "北海 银滩",
                        "page": 1,
                        "size": 50,
                        "source": "WEB",
                        "type": 3,
                    },
                },
            ],
        )
        self.assertEqual(result[0]["poiId"], "poi-1")
        self.assertEqual(manager.browser.context_kwargs, [{"storage_state": str(state)}])
        self.assertEqual(manager.browser.context.close_calls, 1)
        self.assertEqual(manager.browser.close_calls, 1)
        self.assertEqual(manager.exit_calls, 1)

    async def test_search_checks_page_url_and_closes_resources_on_failure(self) -> None:
        from playwright import async_api

        invalid_urls = [
            "https://www.xiaohongshu.com/login",
            (
                "https://creator.xiaohongshu.com.evil.example/"
                "publish/publish?source=official"
            ),
            "http://creator.xiaohongshu.com/publish/publish?source=official",
            "https://creator.xiaohongshu.com/publish/other?source=official",
        ]
        for current_url in invalid_urls:
            with self.subTest(current_url=current_url):
                page = _SearchBoundaryPage(current_url=current_url)
                manager = _SearchBoundaryPlaywrightManager(page)
                with patch.object(
                    xhs_location_service,
                    "_storage_state",
                    return_value=Path("/offline/xhs-11.json"),
                ), patch.object(
                    async_api,
                    "async_playwright",
                    return_value=manager,
                ):
                    with self.assertRaisesRegex(
                        xhs_location_service.XhsLocationSearchError,
                        "会话已失效",
                    ):
                        await xhs_location_service._search(
                            {"id": 11, "type": 1, "filePath": "xhs-11.json"},
                            "北海银滩",
                        )

                self.assertEqual(page.evaluate_calls, [])
                self.assertEqual(manager.browser.context.close_calls, 1)
                self.assertEqual(manager.browser.close_calls, 1)
                self.assertEqual(manager.exit_calls, 1)

    async def test_search_preserves_operation_error_and_closes_browser_when_context_close_fails(
        self,
    ) -> None:
        page = _SearchBoundaryPage(
            current_url="https://www.xiaohongshu.com/login"
        )
        manager = _SearchBoundaryPlaywrightManager(page)
        manager.browser.context.close_error = RuntimeError("context close failed")
        from playwright import async_api

        with patch.object(
            xhs_location_service,
            "_storage_state",
            return_value=Path("/offline/xhs-11.json"),
        ), patch.object(async_api, "async_playwright", return_value=manager):
            with self.assertRaisesRegex(
                xhs_location_service.XhsLocationSearchError,
                "会话已失效",
            ):
                await xhs_location_service._search(
                    {"id": 11, "type": 1, "filePath": "xhs-11.json"},
                    "北海银滩",
                )

        self.assertEqual(manager.browser.context.close_calls, 1)
        self.assertEqual(manager.browser.close_calls, 1)
        self.assertEqual(manager.exit_calls, 1)


class _FakeResponseRequest:
    method = "POST"

    def __init__(self, post_data: object) -> None:
        self._post_data = post_data

    @property
    def post_data_json(self) -> object:
        if self._post_data is _MALFORMED_POST_DATA:
            raise ValueError("malformed request JSON")
        return self._post_data


class _FakeResponse:
    def __init__(
        self,
        page: "_FakeXhsEditorPage",
        payload: object,
        post_data: object,
    ) -> None:
        self.page = page
        self.url = xhs_location_service.XHS_LOCATION_SEARCH_ENDPOINT
        self.request = _FakeResponseRequest(post_data)
        self._payload = payload

    async def json(self) -> object:
        self.page.calls.append("response-json")
        return self._payload


class _FakeResponseInfo:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    @property
    async def value(self) -> _FakeResponse:
        return self._response


class _FakeResponseExpectation:
    def __init__(self, page: "_FakeXhsEditorPage", predicate) -> None:
        self.page = page
        self.predicate = predicate

    async def __aenter__(self) -> _FakeResponseInfo:
        self.page.calls.append("response-listener")
        for index, (post_data, payload) in enumerate(self.page.response_sequence):
            response = _FakeResponse(self.page, payload, post_data)
            if self.predicate(response):
                self.page.calls.append(f"response-match:{index}")
                return _FakeResponseInfo(response)
        raise AssertionError("adapter did not match a current full-keyword creator/search POST")

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _FakeLocator:
    def __init__(self, page: "_FakeXhsEditorPage", nodes: list["_FakeNode"]) -> None:
        self.page = page
        self.nodes = nodes

    @property
    def first(self) -> "_FakeNode":
        return self.nth(0)

    async def count(self) -> int:
        return len(self.nodes)

    def nth(self, index: int) -> "_FakeNode":
        if 0 <= index < len(self.nodes):
            return self.nodes[index]
        return _FakeNode(self.page, "empty", visible=False)

    def filter(self, *, has_text: object = None) -> "_FakeLocator":
        return self


class _FakeNode:
    def __init__(
        self,
        page: "_FakeXhsEditorPage",
        kind: str,
        *,
        text: str = "",
        address: str = "",
        visible: bool = True,
    ) -> None:
        self.page = page
        self.kind = kind
        self.text = text
        self.address = address
        self._visible = visible

    @property
    def first(self) -> "_FakeNode":
        return self

    async def count(self) -> int:
        return 0 if self.kind == "empty" else 1

    def nth(self, index: int) -> "_FakeNode":
        return self if index == 0 else _FakeNode(self.page, "empty", visible=False)

    def filter(self, *, has_text: object = None) -> "_FakeNode":
        return self

    def locator(self, selector: str) -> _FakeLocator:
        if self.kind == "dropdown" and selector == ".option-item":
            return _FakeLocator(
                self.page,
                [
                    _FakeNode(self.page, "option", text=name, address=address)
                    for name, address in self.page.dom_options
                ],
            )
        if self.kind == "option" and selector == ".option-name":
            return _FakeLocator(self.page, [_FakeNode(self.page, "text", text=self.text)])
        if self.kind == "option" and selector == ".option-subname":
            return _FakeLocator(
                self.page, [_FakeNode(self.page, "text", text=self.address)]
            )
        if self.kind == "topic-option" and selector == ".name":
            return _FakeLocator(self.page, [_FakeNode(self.page, "text", text=self.text)])
        if self.kind == "editor" and selector == "a.tiptap-topic":
            return _FakeLocator(
                self.page,
                [_FakeNode(self.page, "text", text=value) for value in self.page.topics],
            )
        if selector == ".d-select.disabled" and self.page.disabled:
            return _FakeLocator(self.page, [_FakeNode(self.page, "disabled")])
        return _FakeLocator(self.page, [])

    async def is_visible(self) -> bool:
        if self.kind in {"dropdown", "option"}:
            return self._visible and self.page.dropdown_visible
        return self._visible

    async def evaluate(self, script: str, *args):
        if "getBoundingClientRect" in script:
            return await self.is_visible()
        if self.kind == "title" and args:
            self.page.title = str(args[0])
            self.page.calls.append("title-write")
            return None
        if self.kind == "editor" and args:
            self.page.body = str(args[0])
            self.page.calls.append("body-write")
            return None
        return None

    async def click(self, **kwargs) -> None:
        if self.kind == "location-trigger":
            self.page.dropdown_visible = True
            self.page.calls.append("location-trigger-click")
        elif self.kind == "option":
            self.page.selected_name = self.page.readback_override or self.text
            self.page.dropdown_visible = False
            self.page.calls.append(f"location-option-click:{self.text}|{self.address}")
        elif self.kind == "topic-option":
            self.page.topics.append(self.text)
            self.page.calls.append("topic-option-click")
        elif self.kind == "editor":
            self.page.calls.append("editor-click")

    async def fill(self, value: str, **kwargs) -> None:
        if self.kind != "location-input":
            raise AssertionError(f"unexpected fill on {self.kind}")
        self.page.calls.append(f"location-input-fill:{value}")

    async def wait_for(self, *, state: str, timeout: int) -> None:
        if self.kind == "loading" and state == "hidden":
            self.page.calls.append("loading-hidden")
            return
        if self.kind == "dropdown" and state == "hidden" and not self.page.dropdown_visible:
            self.page.calls.append("dropdown-hidden")
            return
        if state in {"visible", "attached"} and await self.is_visible():
            return
        raise AssertionError(f"{self.kind} did not reach {state}")

    async def set_input_files(self, paths) -> None:
        self.page.calls.append("media-upload")

    async def inner_text(self, **kwargs) -> str:
        if self.kind == "media-panel":
            return self.page.media_filename
        if self.kind == "editor":
            return self.page.body
        if self.kind == "selected-description":
            return self.page.selected_name
        return self.text

    async def input_value(self) -> str:
        return self.page.title

    async def is_enabled(self) -> bool:
        return True


class _FakeKeyboard:
    def __init__(self, page: "_FakeXhsEditorPage") -> None:
        self.page = page

    async def press(self, value: str) -> None:
        self.page.calls.append(f"keyboard:{value}")

    async def insert_text(self, value: str) -> None:
        self.page.calls.append(f"topic-input:{value}")


class _FakeXhsEditorPage:
    def __init__(
        self,
        response_payload: object,
        dom_options: list[tuple[str, str]],
        *,
        selected_name: str = "",
        disabled: bool = False,
        account_name: str = "海风",
        response_sequence: list[tuple[object, object]] | None = None,
    ) -> None:
        self.response_payload = response_payload
        self.response_sequence = response_sequence or [
            (
                {
                    "latitude": 0,
                    "longitude": 0,
                    "keyword": "北海银滩",
                    "page": 1,
                    "size": 50,
                    "source": "WEB",
                    "type": 3,
                },
                response_payload,
            )
        ]
        self.dom_options = dom_options
        self.selected_name = selected_name
        self.disabled = disabled
        self.account_name = account_name
        self.dropdown_visible = False
        self.readback_override = ""
        self.calls: list[str] = []
        self.title = ""
        self.body = ""
        self.topics: list[str] = []
        self.media_filename = ""
        self.keyboard = _FakeKeyboard(self)

    def locator(self, selector: str, *, has_text: object = None) -> _FakeLocator:
        if selector == ".address-card-wrapper .address-card-select":
            return _FakeLocator(self, [_FakeNode(self, "location-trigger")])
        if selector == ".address-card-wrapper .d-select.disabled":
            nodes = [_FakeNode(self, "disabled")] if self.disabled else []
            return _FakeLocator(self, nodes)
        if selector == '.d-select-input-filter input[type="text"]':
            return _FakeLocator(self, [_FakeNode(self, "location-input")])
        if selector == ".loading-container":
            return _FakeLocator(self, [_FakeNode(self, "loading", visible=False)])
        if selector == ".custom-dropdown-44":
            return _FakeLocator(self, [_FakeNode(self, "dropdown")])
        if selector == ".address-card-select .d-select-description":
            return _FakeLocator(self, [_FakeNode(self, "selected-description")])
        if selector in {".upload-container", ".upload-container .creator-tab"}:
            return _FakeLocator(self, [_FakeNode(self, "upload-container")])
        if selector == (
            ".upload-container .header-tabs "
            ".creator-tab:not([aria-hidden='true'])"
        ):
            return _FakeLocator(self, [])
        if selector == "input.upload-input[type=file]":
            return _FakeLocator(self, [_FakeNode(self, "upload-input")])
        if "input.upload-input[type=file][accept*='video']" in selector:
            return _FakeLocator(self, [_FakeNode(self, "upload-input")])
        if selector == ".publish-page-content-media":
            return _FakeLocator(self, [_FakeNode(self, "media-panel")])
        if selector == ".tiptap.ProseMirror":
            return _FakeLocator(self, [_FakeNode(self, "editor")])
        if 'input[placeholder="填写标题会有更多赞哦"]' in selector:
            return _FakeLocator(self, [_FakeNode(self, "title")])
        if selector == ".tippy-box .items .item":
            return _FakeLocator(
                self, [_FakeNode(self, "topic-option", text="#测试")]
            )
        if selector == ".post-time-wrapper":
            self.calls.append("schedule-check")
            return _FakeLocator(self, [])
        return _FakeLocator(self, [])

    def get_by_text(self, text: str, *, exact: bool) -> _FakeLocator:
        return _FakeLocator(self, [])

    async def goto(self, url: str, **kwargs) -> None:
        self.calls.append(f"goto:{url}")

    async def wait_for_timeout(self, timeout: int) -> None:
        return None

    async def evaluate(self, script: str):
        if "personal_info" in script:
            return {"success": True, "data": {"user": {"nickname": self.account_name}}}
        raise AssertionError("unexpected page.evaluate call")

    def expect_response(self, predicate, *, timeout: int) -> _FakeResponseExpectation:
        return _FakeResponseExpectation(self, predicate)

    async def bring_to_front(self) -> None:
        self.calls.append("bring-to-front")


class _ForbiddenPlaywrightStarter:
    def __init__(self) -> None:
        self.started = False

    async def start(self):
        self.started = True
        raise AssertionError("Playwright must not start for an account ID mismatch")


class _FakePlaywrightContext:
    def __init__(self, page: _FakeXhsEditorPage) -> None:
        self.page = page

    async def new_page(self) -> _FakeXhsEditorPage:
        return self.page

    async def close(self) -> None:
        return None


class _FakePlaywrightBrowser:
    def __init__(self, page: _FakeXhsEditorPage) -> None:
        self.page = page

    async def new_context(self, **kwargs) -> _FakePlaywrightContext:
        return _FakePlaywrightContext(self.page)

    async def close(self) -> None:
        return None


class _FakeChromium:
    def __init__(self, page: _FakeXhsEditorPage) -> None:
        self.page = page

    async def launch(self, **kwargs) -> _FakePlaywrightBrowser:
        return _FakePlaywrightBrowser(self.page)


class _FakePlaywright:
    def __init__(self, page: _FakeXhsEditorPage) -> None:
        self.chromium = _FakeChromium(page)

    async def stop(self) -> None:
        return None


class _FakePlaywrightStarter:
    def __init__(self, page: _FakeXhsEditorPage) -> None:
        self.playwright = _FakePlaywright(page)

    async def start(self) -> _FakePlaywright:
        return self.playwright


class XhsLocationEditorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.video_path = Path(self.temporary.name) / "fixture.mp4"
        self.video_path.write_bytes(b"offline-video-fixture")
        self.image_path = Path(self.temporary.name) / "fixture.png"
        self.image_path.write_bytes(b"offline-image-fixture")
        self.account_file = Path(self.temporary.name) / "xhs-123.json"
        self.account_file.write_text("{}", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _payload(self, **changes: object) -> dict[str, object]:
        selection = {
            "poiId": "poi-1",
            "name": "北海银滩",
            "address": "广西北海市银海区银滩大道",
            "poiType": "0",
            "platform": "xiaohongshu",
            "sourceAccountId": 123,
            "platformType": 1,
            "scope": "platform-default",
            "contentType": "video",
            "searchKeyword": "北海银滩",
        }
        payload: dict[str, object] = {
            "type": 1,
            "contentType": "video",
            "runtimeMode": "preflight",
            "debugDryRun": True,
            "accountList": [str(self.account_file)],
            "accountIds": [123],
            "title": "地点核验测试",
            "description": "只验证当前编辑页地点候选。",
            "fileList": [str(self.video_path)],
            "tags": [],
            "enableTimer": False,
            "scheduleTime": None,
            "aiGenerated": False,
            "originalDeclaration": False,
            "xhsLocationKeyword": "北海银滩",
            "xhsLocationScope": "platform-default",
            "xhsLocationPoi": selection,
        }
        payload.update(changes)
        return payload

    def _response(self, *rows: dict[str, object]) -> dict[str, object]:
        return {"success": True, "data": {"poiList": list(rows)}}

    def _row(self, **changes: object) -> dict[str, object]:
        row: dict[str, object] = {
            "poiId": "poi-1",
            "name": "北海银滩",
            "fullAddress": "广西北海市银海区银滩大道",
            "poiType": 0,
        }
        row.update(changes)
        return row

    def test_contract_stores_only_valid_xhs_location(self) -> None:
        contract = build_native_contract(self._payload())
        self.assertNotIn("locationKeyword", contract)
        self.assertEqual(contract["location"]["poiId"], "poi-1")
        self.assertEqual(contract["location"]["searchKeyword"], "北海银滩")

        no_location = self._payload(
            xhsLocationKeyword="", xhsLocationScope="", xhsLocationPoi=None
        )
        self.assertNotIn("location", build_native_contract(no_location))

    def test_contract_rejects_generic_or_article_location_before_browser(self) -> None:
        with self.assertRaises(XhsNativeAdapterError):
            build_native_contract(
                self._payload(
                    xhsLocationKeyword="",
                    xhsLocationScope="",
                    xhsLocationPoi=None,
                    locationKeyword="北海银滩",
                )
            )
        article = self._payload(
            contentType="article", fileList=[str(self.image_path)]
        )
        with self.assertRaisesRegex(XhsNativeAdapterError, "地点.*视频"):
            build_native_contract(article)

    async def test_no_location_makes_zero_page_calls(self) -> None:
        adapter = XhsNativeAdapter(
            self._payload(
                xhsLocationKeyword="", xhsLocationScope="", xhsLocationPoi=None
            )
        )

        class NoPageCallsAllowed:
            calls = 0

            def __getattribute__(self, name: str):
                if name == "calls":
                    return object.__getattribute__(self, name)
                object.__getattribute__(self, "calls")
                object.__setattr__(self, "calls", self.calls + 1)
                raise AssertionError(f"unexpected page call: {name}")

        page = NoPageCallsAllowed()
        self.assertIsNone(await adapter.apply_location(page))
        self.assertEqual(page.calls, 0)

    async def test_revalidates_three_fields_and_clicks_exact_dom_row(self) -> None:
        page = _FakeXhsEditorPage(
            self._response(
                self._row(
                    poiId="poi-other",
                    fullAddress="广西北海市海城区海景大道",
                ),
                self._row(),
            ),
            [
                ("北海银滩", "广西北海市海城区海景大道"),
                ("北海银滩", "广西北海市银海区银滩大道"),
            ],
        )
        result = await XhsNativeAdapter(self._payload()).apply_location(page)

        self.assertEqual(result["poiId"], "poi-1")
        self.assertEqual(result["name"], "北海银滩")
        self.assertEqual(result["address"], "广西北海市银海区银滩大道")
        self.assertEqual(result["editorNameReadback"], "北海银滩")
        self.assertEqual(result["poiIdEvidence"], "creator-search-response")
        self.assertLess(
            page.calls.index("response-listener"),
            page.calls.index("location-input-fill:北海银滩"),
        )
        self.assertIn(
            "location-option-click:北海银滩|广西北海市银海区银滩大道",
            page.calls,
        )

    async def test_skips_same_endpoint_responses_until_full_keyword_request(self) -> None:
        wrong_response = self._response()
        correct_response = self._response(self._row())
        page = _FakeXhsEditorPage(
            correct_response,
            [("北海银滩", "广西北海市银海区银滩大道")],
            response_sequence=[
                (None, wrong_response),
                (_MALFORMED_POST_DATA, wrong_response),
                (
                    {
                        "latitude": 0,
                        "longitude": 0,
                        "keyword": "",
                        "page": 1,
                        "size": 50,
                        "source": "WEB",
                        "type": 1,
                    },
                    wrong_response,
                ),
                (
                    {
                        "latitude": 22.0,
                        "longitude": 109.0,
                        "keyword": "北海老街",
                        "page": 1,
                        "size": 50,
                        "source": "WEB",
                        "type": 3,
                    },
                    wrong_response,
                ),
                (
                    {
                        "latitude": 22.0,
                        "longitude": 109.0,
                        "keyword": "  北海银滩\n",
                        "page": 1,
                        "size": 50,
                        "source": "WEB",
                        "type": 3,
                    },
                    correct_response,
                ),
            ],
        )

        result = await XhsNativeAdapter(self._payload()).apply_location(page)

        self.assertEqual(result["poiId"], "poi-1")
        self.assertIn("response-match:4", page.calls)

    async def test_rejects_changed_or_ambiguous_response_identity(self) -> None:
        cases = [
            self._response(self._row(poiId="poi-changed")),
            self._response(self._row(fullAddress="广西北海市海城区")),
            self._response(self._row(), self._row()),
            self._response(
                self._row(),
                self._row(poiId="poi-2"),
            ),
            self._response(
                {
                    "poiId": "poi-1",
                    "name": "北海银滩",
                    "poiType": 0,
                }
            ),
        ]
        for response in cases:
            with self.subTest(response=response):
                page = _FakeXhsEditorPage(
                    response,
                    [("北海银滩", "广西北海市银海区银滩大道")],
                )
                with self.assertRaises(XhsNativeAdapterError):
                    await XhsNativeAdapter(self._payload()).apply_location(page)

    async def test_rejects_disabled_multi_location_duplicate_dom_and_bad_readback(self) -> None:
        cases = [
            _FakeXhsEditorPage(
                self._response(self._row()),
                [("北海银滩", "广西北海市银海区银滩大道")],
                disabled=True,
            ),
            _FakeXhsEditorPage(
                self._response(self._row()),
                [("北海银滩", "广西北海市银海区银滩大道")],
                selected_name="北海银滩等 2 个地点",
            ),
            _FakeXhsEditorPage(
                self._response(self._row()),
                [
                    ("北海银滩", "广西北海市银海区银滩大道"),
                    ("北海银滩", "广西北海市银海区银滩大道"),
                ],
            ),
        ]
        for page in cases:
            with self.subTest(calls=page.calls):
                with self.assertRaises(XhsNativeAdapterError):
                    await XhsNativeAdapter(self._payload()).apply_location(page)

        bad_readback = _FakeXhsEditorPage(
            self._response(self._row()),
            [("北海银滩", "广西北海市银海区银滩大道")],
        )
        bad_readback.readback_override = "北海银滩新区"
        with self.assertRaises(XhsNativeAdapterError):
            await XhsNativeAdapter(self._payload()).apply_location(bad_readback)

    async def test_preflight_applies_location_after_content_before_topics(self) -> None:
        payload = self._payload(tags=["测试"])
        page = _FakeXhsEditorPage(
            self._response(self._row()),
            [("北海银滩", "广西北海市银海区银滩大道")],
        )
        page.media_filename = self.video_path.name

        message = await oneclick_preflight._xhs_preflight(page, payload)

        self.assertLess(
            page.calls.index("body-write"),
            page.calls.index("location-option-click:北海银滩|广西北海市银海区银滩大道"),
        )
        self.assertLess(
            page.calls.index("location-option-click:北海银滩|广西北海市银海区银滩大道"),
            page.calls.index("topic-option-click"),
        )
        self.assertIn("未主动保存草稿、未预览、未发布", message)

    async def test_direct_runners_reject_actual_account_id_mismatch_before_playwright(self) -> None:
        account = {
            "id": 123,
            "type": 1,
            "filePath": str(self.account_file),
            "userName": "海风",
        }
        mismatched_selection = dict(self._payload()["xhsLocationPoi"])
        mismatched_selection["sourceAccountId"] = 999
        payload = self._payload(
            accountIds=[999],
            xhsLocationPoi=mismatched_selection,
        )
        with patch.object(account_service, "list_accounts", return_value=[account]):
            from playwright import async_api

            preflight_starter = _ForbiddenPlaywrightStarter()
            with patch.object(async_api, "async_playwright", return_value=preflight_starter):
                with self.assertRaisesRegex(oneclick_preflight.PreflightError, "账号"):
                    await oneclick_preflight.run_preflight(payload)
            self.assertFalse(preflight_starter.started)

            publish_payload = dict(payload)
            publish_payload.update(runtimeMode="publish", debugDryRun=False)
            publish_starter = _ForbiddenPlaywrightStarter()
            with patch.object(async_api, "async_playwright", return_value=publish_starter):
                with self.assertRaisesRegex(XhsPublishError, "账号"):
                    await run_xhs_publish(publish_payload, task_id=1)
            self.assertFalse(publish_starter.started)

    async def test_formal_publish_applies_location_after_content_before_topics(self) -> None:
        payload = self._payload(
            runtimeMode="publish",
            debugDryRun=False,
            tags=["测试"],
        )
        page = _FakeXhsEditorPage(
            self._response(self._row()),
            [("北海银滩", "广西北海市银海区银滩大道")],
        )
        page.media_filename = self.video_path.name
        account = {
            "id": 123,
            "type": 1,
            "filePath": str(self.account_file),
            "userName": "海风",
        }
        from playwright import async_api

        with (
            patch.object(account_service, "list_accounts", return_value=[account]),
            patch.object(oneclick_preflight, "COOKIE_DIR", Path(self.temporary.name)),
            patch.object(
                async_api,
                "async_playwright",
                return_value=_FakePlaywrightStarter(page),
            ),
        ):
            with self.assertRaisesRegex(XhsPublishError, "话题节点"):
                await run_xhs_publish(payload, task_id=1)

        self.assertLess(
            page.calls.index("body-write"),
            page.calls.index("location-option-click:北海银滩|广西北海市银海区银滩大道"),
        )
        self.assertLess(
            page.calls.index("location-option-click:北海银滩|广西北海市银海区银滩大道"),
            page.calls.index("topic-option-click"),
        )


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
