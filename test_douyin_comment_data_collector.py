# -*- coding: utf-8 -*-
"""抖音评论只读采集器的合同、隐私和硬生命周期测试。"""

from __future__ import annotations

import asyncio
from contextlib import redirect_stderr
from dataclasses import replace
import gc
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app_core.douyin_comment_data_collector import (
    DouyinCommentDataCollector,
    parse_comment_page,
)
from app_core.platform_data_comment_contract import DouyinCommentContract
from app_core.platform_data_comment_models import (
    CommentInsightFailure,
    derive_comment_key,
)


OBSERVED_AT = "2026-08-23T10:21:00+08:00"


def verified_contract(**overrides: object) -> DouyinCommentContract:
    values = dict(
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
    values.update(overrides)
    return DouyinCommentContract(**values)


def comment_row(
    comment_id: object = "comment-1",
    *,
    content_id: object = "work-7",
    parent_id: object = None,
    body: object = "为什么会这样？",
    like_count: object = 3,
    reply_count: object = 1,
    commented_at: object = "2026-08-23T10:20:30+08:00",
    author: object | None = None,
) -> dict:
    return {
        "comment_id": comment_id,
        "content_id": content_id,
        "parent_id": parent_id,
        "text": body,
        "like_count": like_count,
        "reply_count": reply_count,
        "commented_at": commented_at,
        "author": author
        if author is not None
        else {"uid": "private-default", "nickname": "private-default"},
    }


def comment_payload(
    *rows: dict,
    cursor: object = "",
    has_more: object = False,
) -> dict:
    return {
        "data": {
            "comments": list(rows),
            "cursor": cursor,
            "has_more": has_more,
        }
    }


def production_exception_artifacts(error: BaseException) -> tuple[str, tuple[object, ...]]:
    """只检查生产采集器 traceback，避免把测试自己的 fixture 当成泄漏。"""

    encoded: list[str] = []
    values: list[object] = []
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        encoded.append(repr(current.args))
        traceback = current.__traceback__
        while traceback is not None:
            frame = traceback.tb_frame
            if frame.f_code.co_filename.endswith(
                "app_core/douyin_comment_data_collector.py"
            ):
                for value in frame.f_locals.values():
                    values.append(value)
                    try:
                        encoded.append(repr(value))
                    except BaseException:
                        encoded.append("<repr-failed>")
            traceback = traceback.tb_next
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return "\n".join(encoded), tuple(values)


def production_thread_artifacts(thread_name: str) -> str:
    """检查指定生产 worker 的全部当前栈帧，不扫测试线程。"""

    frames = sys._current_frames()
    encoded: list[str] = []
    for thread in threading.enumerate():
        if thread.name != thread_name or thread.ident is None:
            continue
        frame = frames.get(thread.ident)
        while frame is not None:
            if frame.f_code.co_filename.endswith(
                "app_core/douyin_comment_data_collector.py"
            ):
                for value in frame.f_locals.values():
                    try:
                        encoded.append(repr(value))
                    except BaseException:
                        encoded.append("<repr-failed>")
            frame = frame.f_back
    return "\n".join(encoded)


class DouyinCommentParserTests(unittest.TestCase):
    def test_parser_hashes_id_discards_author_and_keeps_only_top_level(self) -> None:
        """保留平台 ID、作者对象或子回复任一项都会泄露越界数据。"""

        page = parse_comment_page(
            verified_contract(),
            comment_payload(
                comment_row(
                    "raw-top-id",
                    author={"uid": "private-uid", "nickname": "private-name"},
                ),
                comment_row("raw-child-id", parent_id="raw-top-id"),
            ),
            account_id=12,
            content_id="work-7",
            observed_at=OBSERVED_AT,
        )

        self.assertEqual(len(page.comments), 1)
        record = page.comments[0]
        self.assertEqual(
            record.comment_key,
            derive_comment_key(12, "work-7", "raw-top-id"),
        )
        self.assertEqual(record.body, "为什么会这样？")
        self.assertEqual((record.like_count, record.reply_count), (3, 1))
        self.assertEqual(record.commented_at, "2026-08-23T10:20:30+08:00")
        encoded = repr(page)
        for forbidden in (
            "raw-top-id",
            "raw-child-id",
            "private-uid",
            "private-name",
            "private-default",
            "author",
            "nickname",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_malformed_rows_are_rejected_individually(self) -> None:
        """一条普通坏记录不能污染同页好记录。"""

        page = parse_comment_page(
            verified_contract(),
            comment_payload(
                comment_row("good"),
                comment_row("bad-like", like_count=-1),
                comment_row("bad-bool", reply_count=True),
            ),
            account_id=12,
            content_id="work-7",
            observed_at=OBSERVED_AT,
        )

        self.assertEqual(
            [item.comment_key for item in page.comments],
            [derive_comment_key(12, "work-7", "good")],
        )
        self.assertTrue(page.platform_end)

    def test_nonterminal_page_without_one_valid_top_level_row_fails_closed(self) -> None:
        """非终页若只有坏记录或子回复，不能假装成可继续的空页。"""

        with self.assertRaises(CommentInsightFailure) as raised:
            parse_comment_page(
                verified_contract(),
                comment_payload(
                    comment_row("bad", like_count=-1),
                    comment_row("child", parent_id="parent"),
                    cursor="cursor-1",
                    has_more=True,
                ),
                account_id=12,
                content_id="work-7",
                observed_at=OBSERVED_AT,
            )

        self.assertEqual(raised.exception.error_code, "comment_payload_invalid")
        self.assertEqual(str(raised.exception), "comment_payload_invalid")

    def test_identical_duplicate_ids_dedupe_but_conflicts_fail_closed(self) -> None:
        """重复响应只能合并完全相同的评论，不能静默覆盖计数。"""

        duplicate = comment_row("same-id")
        page = parse_comment_page(
            verified_contract(),
            comment_payload(duplicate, json.loads(json.dumps(duplicate))),
            account_id=12,
            content_id="work-7",
            observed_at=OBSERVED_AT,
        )
        self.assertEqual(len(page.comments), 1)

        with self.assertRaises(CommentInsightFailure) as raised:
            parse_comment_page(
                verified_contract(),
                comment_payload(
                    comment_row("same-id", like_count=1),
                    comment_row("same-id", like_count=2),
                ),
                account_id=12,
                content_id="work-7",
                observed_at=OBSERVED_AT,
            )
        self.assertEqual(raised.exception.error_code, "comment_payload_invalid")

    def test_more_pages_require_strict_cursor_and_builtin_json_containers(self) -> None:
        """游标缺失、弱类型或容器子类不得诱导采集器猜测翻页。"""

        class CustomDict(dict):
            pass

        class CustomString(str):
            pass

        for payload in (
            comment_payload(comment_row(), cursor="", has_more=True),
            comment_payload(comment_row(), cursor=True, has_more=True),
            comment_payload(comment_row(), cursor=" cursor ", has_more=True),
            comment_payload(
                comment_row(), cursor=CustomString(""), has_more=False
            ),
            CustomDict(comment_payload(comment_row())),
        ):
            with self.subTest(payload_type=type(payload).__name__):
                with self.assertRaises(CommentInsightFailure) as raised:
                    parse_comment_page(
                        verified_contract(),
                        payload,
                        account_id=12,
                        content_id="work-7",
                        observed_at=OBSERVED_AT,
                    )
                self.assertEqual(
                    raised.exception.error_code, "comment_payload_invalid"
                )
                self.assertEqual(
                    str(raised.exception), "comment_payload_invalid"
                )


async def wait_forever_ignoring_cancellation() -> None:
    blocker = asyncio.Event()
    while True:
        try:
            await blocker.wait()
        except asyncio.CancelledError:
            continue


class FakeRequest:
    def __init__(self, method: str = "GET") -> None:
        self.method = method


_DEFAULT_RESPONSE_HEADERS = object()


class FakeResponse:
    def __init__(
        self,
        payload: object,
        *,
        url: str = "https://creator.douyin.com/verified/comment/list",
        method: str = "GET",
        status: int = 200,
        delay: float = 0.0,
        error: BaseException | None = None,
        resist_cancellation: bool = False,
        headers: object = _DEFAULT_RESPONSE_HEADERS,
        headers_after_first_read: object = _DEFAULT_RESPONSE_HEADERS,
    ) -> None:
        self.url = url
        self.request = FakeRequest(method)
        self.status = status
        self._headers = (
            {"content-type": "application/json"}
            if headers is _DEFAULT_RESPONSE_HEADERS
            else headers
        )
        self._headers_after_first_read = headers_after_first_read
        self.header_reads = 0
        self.payload = payload
        self.delay = delay
        self.error = error
        self.resist_cancellation = resist_cancellation
        self.json_calls = 0

    @property
    def headers(self) -> object:
        self.header_reads += 1
        if (
            self.header_reads > 1
            and self._headers_after_first_read is not _DEFAULT_RESPONSE_HEADERS
        ):
            return self._headers_after_first_read
        return self._headers

    async def json(self) -> object:
        self.json_calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.resist_cancellation:
            await wait_forever_ignoring_cancellation()
        if self.error is not None:
            raise self.error
        return self.payload


class FakeLocator:
    def __init__(self, page: "FakePage", target: str) -> None:
        self.page = page
        self.target = target

    async def click(self, *, timeout: float) -> None:
        self.page.trigger_calls.append(("click", self.target, timeout))
        await self.page.emit_next_page()

    async def scroll_into_view_if_needed(self, *, timeout: float) -> None:
        self.page.trigger_calls.append(("scroll", self.target, timeout))
        await self.page.emit_next_page()


class FakePage:
    def __init__(
        self,
        response_pages: tuple[tuple[FakeResponse, ...], ...],
        *,
        final_url: str = "https://creator.douyin.com/verified/content/work-7/comments",
        goto_delay: float = 0.0,
        goto_error: BaseException | None = None,
        locator_error: BaseException | None = None,
        close_delay: float = 0.0,
        close_error: BaseException | None = None,
        close_resists_cancellation: bool = False,
    ) -> None:
        self.response_pages = response_pages
        self.url = final_url
        self.goto_delay = goto_delay
        self.goto_error = goto_error
        self.locator_error = locator_error
        self.close_delay = close_delay
        self.close_error = close_error
        self.close_resists_cancellation = close_resists_cancellation
        self.listeners: dict[str, object] = {}
        self.listener_history: list[object] = []
        self.listener_removals = 0
        self.goto_calls: list[tuple[str, str, float]] = []
        self.locator_calls: list[str] = []
        self.trigger_calls: list[tuple[str, str, float]] = []
        self.next_page_index = 0
        self.closed = 0

    def on(self, event: str, callback) -> None:
        self.listeners[event] = callback
        self.listener_history.append(callback)

    def remove_listener(self, event: str, callback) -> None:
        if self.listeners.get(event) is callback:
            self.listeners.pop(event)
        self.listener_removals += 1

    async def _emit_page(self, index: int) -> None:
        if index >= len(self.response_pages):
            return
        for response in self.response_pages[index]:
            self.listeners["response"](response)
            await asyncio.sleep(0)

    async def emit_next_page(self) -> None:
        self.next_page_index += 1
        await self._emit_page(self.next_page_index)

    async def goto(
        self,
        url: str,
        *,
        wait_until: str,
        timeout: float,
    ) -> None:
        if "response" not in self.listeners:
            raise AssertionError("listener must be installed before navigation")
        self.goto_calls.append((url, wait_until, timeout))
        if self.goto_delay:
            await asyncio.sleep(self.goto_delay)
        if self.goto_error is not None:
            raise self.goto_error
        await self._emit_page(0)
        # 让响应 worker 真正进入 json() 再返回导航，避免测试只取消到空队列等待。
        await asyncio.sleep(0)

    def locator(self, target: str) -> FakeLocator:
        self.locator_calls.append(target)
        if self.locator_error is not None:
            raise self.locator_error
        return FakeLocator(self, target)

    async def close(self) -> None:
        self.closed += 1
        if self.close_delay:
            await asyncio.sleep(self.close_delay)
        if self.close_resists_cancellation:
            await wait_forever_ignoring_cancellation()
        if self.close_error is not None:
            raise self.close_error


class FakeContext:
    def __init__(
        self,
        page: FakePage,
        *,
        page_delay: float = 0.0,
        page_error: BaseException | None = None,
        page_after_cancel_delay: float | None = None,
        close_delay: float = 0.0,
        close_error: BaseException | None = None,
    ) -> None:
        self.page = page
        self.page_delay = page_delay
        self.page_error = page_error
        self.page_after_cancel_delay = page_after_cancel_delay
        self.close_delay = close_delay
        self.close_error = close_error
        self.storage_states: list[str] = []
        self.closed = 0

    async def new_page(self) -> FakePage:
        if self.page_delay:
            try:
                await asyncio.sleep(self.page_delay)
            except asyncio.CancelledError:
                if self.page_after_cancel_delay is None:
                    raise
                await asyncio.sleep(self.page_after_cancel_delay)
        if self.page_error is not None:
            raise self.page_error
        return self.page

    async def close(self) -> None:
        self.closed += 1
        if self.close_delay:
            await asyncio.sleep(self.close_delay)
        if self.close_error is not None:
            raise self.close_error


class FakeBrowser:
    def __init__(
        self,
        context: FakeContext,
        *,
        context_delay: float = 0.0,
        context_error: BaseException | None = None,
        context_after_cancel_delay: float | None = None,
        close_delay: float = 0.0,
        close_error: BaseException | None = None,
    ) -> None:
        self.context = context
        self.context_delay = context_delay
        self.context_error = context_error
        self.context_after_cancel_delay = context_after_cancel_delay
        self.close_delay = close_delay
        self.close_error = close_error
        self.closed = 0

    async def new_context(self, *, storage_state: str) -> FakeContext:
        self.context.storage_states.append(storage_state)
        if self.context_delay:
            try:
                await asyncio.sleep(self.context_delay)
            except asyncio.CancelledError:
                if self.context_after_cancel_delay is None:
                    raise
                await asyncio.sleep(self.context_after_cancel_delay)
        if self.context_error is not None:
            raise self.context_error
        return self.context

    async def close(self) -> None:
        self.closed += 1
        if self.close_delay:
            await asyncio.sleep(self.close_delay)
        if self.close_error is not None:
            raise self.close_error


class FakeChromium:
    def __init__(
        self,
        browser: FakeBrowser,
        *,
        launch_delay: float = 0.0,
        launch_error: BaseException | None = None,
        launch_after_cancel_delay: float | None = None,
    ) -> None:
        self.browser = browser
        self.launch_delay = launch_delay
        self.launch_error = launch_error
        self.launch_after_cancel_delay = launch_after_cancel_delay
        self.launch_calls: list[dict] = []

    async def launch(self, **options) -> FakeBrowser:
        self.launch_calls.append(dict(options))
        if self.launch_delay:
            try:
                await asyncio.sleep(self.launch_delay)
            except asyncio.CancelledError:
                if self.launch_after_cancel_delay is None:
                    raise
                await asyncio.sleep(self.launch_after_cancel_delay)
        if self.launch_error is not None:
            raise self.launch_error
        return self.browser


class FakePlaywright:
    def __init__(
        self,
        browser: FakeBrowser,
        *,
        launch_delay: float = 0.0,
        launch_error: BaseException | None = None,
        launch_after_cancel_delay: float | None = None,
        stop_delay: float = 0.0,
        stop_error: BaseException | None = None,
    ) -> None:
        self.chromium = FakeChromium(
            browser,
            launch_delay=launch_delay,
            launch_error=launch_error,
            launch_after_cancel_delay=launch_after_cancel_delay,
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


class FakeStarter:
    def __init__(
        self,
        playwright: FakePlaywright,
        *,
        start_delay: float = 0.0,
        start_error: BaseException | None = None,
        start_after_cancel_delay: float | None = None,
    ) -> None:
        self.playwright = playwright
        self.start_delay = start_delay
        self.start_error = start_error
        self.start_after_cancel_delay = start_after_cancel_delay

    async def start(self) -> FakePlaywright:
        if self.start_delay:
            try:
                await asyncio.sleep(self.start_delay)
            except asyncio.CancelledError:
                if self.start_after_cancel_delay is None:
                    raise
                await asyncio.sleep(self.start_after_cancel_delay)
        if self.start_error is not None:
            raise self.start_error
        return self.playwright


class DouyinCommentCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.cookie_dir = Path(self.tempdir.name)
        self.state_file = self.cookie_dir / "oneclick_3_comment.json"
        self.state_file.write_text(
            json.dumps({"cookies": [], "origins": []}), encoding="utf-8"
        )
        self.account = {
            "id": 12,
            "type": 3,
            "filePath": self.state_file.name,
            "userName": "private-account-name",
        }
        self.cookie_patch = patch(
            "app_core.douyin_comment_data_collector.COOKIE_DIR",
            self.cookie_dir,
        )
        self.cookie_patch.start()

    def tearDown(self) -> None:
        self.cookie_patch.stop()
        self.tempdir.cleanup()

    @staticmethod
    def _harness(
        response_pages: tuple[tuple[FakeResponse, ...], ...],
        **page_options: object,
    ) -> tuple[FakeStarter, FakePage, FakeContext, FakeBrowser, FakePlaywright]:
        page = FakePage(response_pages, **page_options)
        context = FakeContext(page)
        browser = FakeBrowser(context)
        playwright = FakePlaywright(browser)
        return FakeStarter(playwright), page, context, browser, playwright

    @staticmethod
    def _assert_closed(
        page: FakePage,
        context: FakeContext,
        browser: FakeBrowser,
        playwright: FakePlaywright,
    ) -> None:
        assert (page.closed, context.closed, browser.closed, playwright.stopped) == (
            1,
            1,
            1,
            1,
        )

    def _collector(
        self,
        starter: FakeStarter,
        *,
        timeout: float = 0.2,
        contract_loader=verified_contract,
    ) -> DouyinCommentDataCollector:
        return DouyinCommentDataCollector(
            contract_loader=contract_loader,
            playwright_factory=lambda: starter,
            total_timeout_seconds=timeout,
            observed_at_factory=lambda: OBSERVED_AT,
        )

    @staticmethod
    def _run_with_wall_limit(
        callback,
        *,
        wall_limit: float = 0.18,
    ) -> tuple[dict[str, object], float, str, bool]:
        outcome: dict[str, object] = {}

        def invoke() -> None:
            try:
                outcome["value"] = callback()
            except BaseException as exc:
                outcome["error"] = exc

        runner = threading.Thread(
            target=invoke,
            name="douyin-comment-bounded-call",
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

    def test_first_sync_is_passive_exact_and_private_data_never_leaves_batch(self) -> None:
        """监听晚装、放宽 authority/path/method 或保留响应都会破坏只读隐私边界。"""

        ignored = (
            FakeResponse(
                comment_payload(comment_row("evil-id", body="evil-body")),
                url="https://evil.example/verified/comment/list",
            ),
            FakeResponse(
                comment_payload(comment_row("userinfo-id")),
                url="https://user:private@creator.douyin.com/verified/comment/list",
            ),
            FakeResponse(
                comment_payload(comment_row("port-id")),
                url="https://creator.douyin.com:444/verified/comment/list",
            ),
            FakeResponse(
                comment_payload(comment_row("wrong-path-id")),
                url="https://creator.douyin.com/unverified/comment/list",
            ),
            FakeResponse(
                comment_payload(comment_row("wrong-method-id")),
                method="POST",
            ),
        )
        accepted = FakeResponse(
            comment_payload(
                comment_row(
                    "raw-comment-1",
                    body="  保留正文空格  ",
                    author={"uid": "private-uid", "nickname": "private-name"},
                )
            ),
            url="https://creator.douyin.com/verified/comment/list?cursor=private",
        )
        starter, page, context, browser, playwright = self._harness(
            ((*ignored, accepted),)
        )
        reports: list[dict] = []

        batch = self._collector(starter).collect(
            self.account,
            "work-7",
            frozenset(),
            report=reports.append,
        )

        self.assertEqual(batch.source_mode, "browser_signed")
        self.assertEqual(batch.stop_reason, "platform_end")
        self.assertEqual(batch.page_count, 1)
        self.assertEqual(batch.accepted_count, 1)
        self.assertEqual(batch.comments[0].body, "  保留正文空格  ")
        self.assertEqual(
            batch.comments[0].comment_key,
            derive_comment_key(12, "work-7", "raw-comment-1"),
        )
        self.assertEqual([item.json_calls for item in ignored], [0] * 5)
        self.assertEqual(accepted.json_calls, 1)
        self.assertEqual(
            page.goto_calls,
            [
                (
                    "https://creator.douyin.com/verified/content/work-7/comments",
                    "domcontentloaded",
                    200.0,
                )
            ],
        )
        self.assertEqual(page.trigger_calls, [])
        self.assertEqual(page.listener_removals, 1)
        self._assert_closed(page, context, browser, playwright)
        self.assertEqual(len(reports), 1)
        encoded = repr((batch, reports))
        for forbidden in (
            "raw-comment-1",
            "private-uid",
            "private-name",
            "private-account-name",
            "cursor=private",
            str(self.state_file),
            "evil-body",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_known_comment_is_included_once_then_stops_before_pagination(self) -> None:
        """遇到第一条已知评论后还点下一页，会多读未授权数据。"""

        known = derive_comment_key(12, "work-7", "old-2")
        first = FakeResponse(
            comment_payload(
                comment_row("new-1"),
                comment_row("old-2", like_count=9, reply_count=4),
                comment_row("must-not-accept-after-known"),
                cursor="cursor-1",
                has_more=True,
            )
        )
        second = FakeResponse(comment_payload(comment_row("must-not-read")))
        starter, page, context, browser, playwright = self._harness(
            ((first,), (second,))
        )

        batch = self._collector(starter).collect(
            self.account, "work-7", frozenset({known})
        )

        self.assertEqual(batch.stop_reason, "known_comment")
        self.assertEqual(batch.page_count, 1)
        self.assertEqual(batch.accepted_count, 2)
        self.assertEqual(batch.comments[-1].comment_key, known)
        self.assertEqual(
            (batch.comments[-1].like_count, batch.comments[-1].reply_count),
            (9, 4),
        )
        self.assertEqual(page.trigger_calls, [])
        self.assertEqual(second.json_calls, 0)
        self._assert_closed(page, context, browser, playwright)

    def test_non_2xx_exact_contract_responses_fail_before_reading_json(self) -> None:
        """精确接口的重定向或失败状态不能被当成成功评论页。"""

        cases = (
            (301, "comment_payload_invalid"),
            (400, "comment_payload_invalid"),
            (401, "comment_login_required"),
            (403, "comment_access_denied"),
            (404, "comment_payload_invalid"),
            (500, "comment_payload_invalid"),
        )
        for status, expected_code in cases:
            with self.subTest(status=status):
                response = FakeResponse(
                    comment_payload(comment_row(f"status-{status}")),
                    status=status,
                )
                starter, page, context, browser, playwright = self._harness(
                    ((response,),)
                )

                with self.assertRaises(CommentInsightFailure) as raised:
                    self._collector(starter).collect(
                        self.account, "work-7", frozenset()
                    )

                self.assertEqual(raised.exception.error_code, expected_code)
                self.assertEqual(response.json_calls, 0)
                self._assert_closed(page, context, browser, playwright)

    def test_content_type_gate_accepts_only_standard_json_media_types(self) -> None:
        """只有标准 JSON 媒体类型可读正文，其他响应头必须在 JSON 前失败。"""

        huge_content_type = "secret-content-type-" + (
            "x" * (64 * 1024 * 1024 - len("secret-content-type-"))
        )
        cases = (
            ("json", {"content-type": "application/json"}, True),
            (
                "json_charset",
                {"content-type": "Application/JSON; charset=utf-8"},
                True,
            ),
            (
                "problem_json",
                {"content-type": "application/problem+json"},
                True,
            ),
            (
                "vendor_json_charset",
                {
                    "content-type": (
                        "application/vnd.douyin.comments+json; charset=UTF-8"
                    )
                },
                True,
            ),
            ("missing", {}, False),
            ("non_container", (), False),
            ("non_string", {"content-type": b"application/json"}, False),
            ("text_html", {"content-type": "text/html"}, False),
            ("text_json", {"content-type": "text/json"}, False),
            ("wildcard", {"content-type": "*/*"}, False),
            (
                "structured_wildcard",
                {"content-type": "application/*+json"},
                False,
            ),
            (
                "duplicate_header",
                {
                    "content-type": "application/json",
                    "Content-Type": "application/problem+json",
                },
                False,
            ),
            (
                "duplicate_charset",
                {
                    "content-type": (
                        "application/json; charset=utf-8; charset=ascii"
                    )
                },
                False,
            ),
            (
                "unsupported_parameter",
                {"content-type": "application/json; boundary=x"},
                False,
            ),
            ("oversized", {"content-type": huge_content_type}, False),
        )
        for name, headers, accepted in cases:
            with self.subTest(name=name):
                response = FakeResponse(
                    comment_payload(comment_row(f"content-type-{name}")),
                    headers=headers,
                )
                starter, page, context, browser, playwright = self._harness(
                    ((response,),)
                )
                timeout = 0.005 if name == "oversized" else 0.2
                started_at = time.monotonic()

                if accepted:
                    batch = self._collector(starter, timeout=timeout).collect(
                        self.account, "work-7", frozenset()
                    )
                    self.assertEqual(batch.accepted_count, 1)
                    self.assertEqual(response.json_calls, 1)
                else:
                    with self.assertRaises(CommentInsightFailure) as raised:
                        self._collector(starter, timeout=timeout).collect(
                            self.account, "work-7", frozenset()
                        )
                    self.assertEqual(
                        raised.exception.error_code,
                        "comment_payload_invalid",
                    )
                    self.assertEqual(response.json_calls, 0)
                    self.assertTrue(raised.exception.cleanup_receipt.closed)
                    self.assertEqual(
                        raised.exception.cleanup_receipt.alive_resource_count,
                        0,
                    )
                    artifacts, values = production_exception_artifacts(
                        raised.exception
                    )
                    self.assertNotIn("secret-content-type-", artifacts)
                    self.assertNotIn(response, values)
                elapsed = time.monotonic() - started_at
                if name == "oversized":
                    self.assertLess(elapsed, 0.03)
                self._assert_closed(page, context, browser, playwright)

    def test_content_type_is_revalidated_after_enqueue_before_json(self) -> None:
        """响应入队后若类型变化，二次检查必须阻止正文读取。"""

        response = FakeResponse(
            comment_payload(comment_row("must-not-read-after-header-change")),
            headers={"content-type": "application/json"},
            headers_after_first_read={"content-type": "text/html"},
        )
        starter, page, context, browser, playwright = self._harness(
            ((response,),)
        )

        with self.assertRaises(CommentInsightFailure) as raised:
            self._collector(starter).collect(
                self.account, "work-7", frozenset()
            )

        self.assertEqual(raised.exception.error_code, "comment_payload_invalid")
        self.assertEqual(response.header_reads, 2)
        self.assertEqual(response.json_calls, 0)
        self._assert_closed(page, context, browser, playwright)

    def test_401_and_403_mapping_precedes_content_type_validation(self) -> None:
        """登录和权限状态必须在读正文前固定映射，不受响应类型干扰。"""

        for status, expected_code in (
            (401, "comment_login_required"),
            (403, "comment_access_denied"),
        ):
            with self.subTest(status=status):
                response = FakeResponse(
                    comment_payload(comment_row(f"status-{status}")),
                    status=status,
                    headers={},
                )
                starter, page, context, browser, playwright = self._harness(
                    ((response,),)
                )

                with self.assertRaises(CommentInsightFailure) as raised:
                    self._collector(starter).collect(
                        self.account, "work-7", frozenset()
                    )

                self.assertEqual(raised.exception.error_code, expected_code)
                self.assertEqual(response.json_calls, 0)
                self._assert_closed(page, context, browser, playwright)

    def test_wrong_or_mixed_content_rows_fail_the_entire_page(self) -> None:
        """任一作品归属不符都必须丢弃整页，不能只计作普通坏行。"""

        payloads = (
            comment_payload(comment_row("wrong-only", content_id="work-8")),
            comment_payload(
                comment_row("right", content_id="work-7"),
                comment_row("wrong-mixed", content_id="work-8"),
            ),
            comment_payload(
                comment_row("right-before-nested"),
                comment_row(
                    "wrong-nested",
                    content_id="work-8",
                    parent_id="parent",
                ),
            ),
        )
        for payload in payloads:
            with self.subTest(row_count=len(payload["data"]["comments"])):
                response = FakeResponse(payload)
                starter, page, context, browser, playwright = self._harness(
                    ((response,),)
                )

                with self.assertRaises(CommentInsightFailure) as raised:
                    self._collector(starter).collect(
                        self.account, "work-7", frozenset()
                    )

                self.assertEqual(
                    raised.exception.error_code, "comment_payload_invalid"
                )
                self._assert_closed(page, context, browser, playwright)

    def test_missing_or_non_string_content_id_fails_the_entire_page(self) -> None:
        """顶层或回复行缺失/弱类型作品 ID 都不是普通坏行。"""

        missing_top = comment_row("missing-top")
        missing_top.pop("content_id")
        missing_reply = comment_row(
            "missing-reply", parent_id="parent"
        )
        missing_reply.pop("content_id")
        payloads = (
            comment_payload(comment_row("valid-a"), missing_top),
            comment_payload(
                comment_row("valid-b"),
                comment_row("integer-top", content_id=7),
            ),
            comment_payload(comment_row("valid-c"), missing_reply),
            comment_payload(
                comment_row("valid-d"),
                comment_row(
                    "integer-reply", content_id=7, parent_id="parent"
                ),
            ),
        )
        for payload in payloads:
            with self.subTest(rows=payload["data"]["comments"][-1]):
                response = FakeResponse(payload)
                starter, page, context, browser, playwright = self._harness(
                    ((response,),)
                )

                with self.assertRaises(CommentInsightFailure) as raised:
                    self._collector(starter).collect(
                        self.account, "work-7", frozenset()
                    )

                self.assertEqual(
                    raised.exception.error_code, "comment_payload_invalid"
                )
                self._assert_closed(page, context, browser, playwright)

    def test_wrong_content_in_row_102_fails_before_accepting_first_101(self) -> None:
        """前 101 条可投影也不得掩盖第 102 条作品归属错误。"""

        rows = [comment_row(f"bound-{index}") for index in range(101)]
        rows.append(comment_row("wrong-102", content_id="work-8"))
        response = FakeResponse(comment_payload(*rows))
        starter, page, context, browser, playwright = self._harness(
            ((response,),)
        )

        with self.assertRaises(CommentInsightFailure) as raised:
            self._collector(starter).collect(
                self.account, "work-7", frozenset()
            )

        self.assertEqual(
            raised.exception.error_code, "comment_payload_invalid"
        )
        self._assert_closed(page, context, browser, playwright)

    def test_wrong_content_well_after_projection_limit_fails_entire_page(self) -> None:
        """投影上限之后很远的作品错误也必须被扫描到。"""

        rows = [comment_row(f"deep-{index}") for index in range(199)]
        rows.append(comment_row("wrong-200", content_id="work-8"))
        response = FakeResponse(comment_payload(*rows))
        starter, page, context, browser, playwright = self._harness(
            ((response,),)
        )

        with self.assertRaises(CommentInsightFailure) as raised:
            self._collector(starter).collect(
                self.account, "work-7", frozenset()
            )

        self.assertEqual(
            raised.exception.error_code, "comment_payload_invalid"
        )
        self._assert_closed(page, context, browser, playwright)

    def test_oversized_raw_page_fails_before_partial_acceptance(self) -> None:
        """超过安全检查上限的整页必须先失败，不返回前 100 条。"""

        response = FakeResponse(
            comment_payload(
                *(comment_row(f"oversized-page-{index}") for index in range(257))
            )
        )
        starter, page, context, browser, playwright = self._harness(
            ((response,),)
        )

        with self.assertRaises(CommentInsightFailure) as raised:
            self._collector(starter).collect(
                self.account, "work-7", frozenset()
            )

        self.assertEqual(
            raised.exception.error_code, "comment_payload_invalid"
        )
        self._assert_closed(page, context, browser, playwright)

    def test_verified_ui_pagination_dedupes_repeated_rows_and_reaches_end(self) -> None:
        """翻页必须仅用合同里的 UI 动作，重复评论在批次中只出现一次。"""

        first = FakeResponse(
            comment_payload(
                comment_row("comment-1"),
                cursor="cursor-1",
                has_more=True,
            )
        )
        second = FakeResponse(
            comment_payload(
                comment_row("comment-1"),
                comment_row("comment-2"),
            )
        )
        starter, page, context, browser, playwright = self._harness(
            ((first,), (second,))
        )

        batch = self._collector(starter).collect(
            self.account, "work-7", frozenset()
        )

        self.assertEqual(batch.stop_reason, "platform_end")
        self.assertEqual(batch.page_count, 2)
        self.assertEqual(batch.accepted_count, 2)
        self.assertEqual(
            [item.comment_key for item in batch.comments],
            [
                derive_comment_key(12, "work-7", "comment-1"),
                derive_comment_key(12, "work-7", "comment-2"),
            ],
        )
        self.assertEqual(page.locator_calls, ["verified-comment-load-more"])
        self.assertEqual(len(page.trigger_calls), 1)
        self.assertEqual(page.trigger_calls[0][:2], ("click", "verified-comment-load-more"))
        self._assert_closed(page, context, browser, playwright)

    def test_exact_hard_max_100_warns_and_never_triggers_page_101(self) -> None:
        """第 100 条必须安全截断，不能被误报为平台已结束。"""

        rows = tuple(comment_row(f"comment-{index}") for index in range(100))
        first = FakeResponse(
            comment_payload(*rows, cursor="cursor-100", has_more=True)
        )
        after_limit = FakeResponse(comment_payload(comment_row("comment-101")))
        starter, page, context, browser, playwright = self._harness(
            ((first,), (after_limit,))
        )

        batch = self._collector(starter).collect(
            self.account, "work-7", frozenset(), limit=100
        )

        self.assertEqual(batch.accepted_count, 100)
        self.assertEqual(batch.stop_reason, "limit_reached")
        self.assertEqual(batch.warning_code, "comment_limit_reached")
        self.assertEqual(batch.page_count, 1)
        self.assertEqual(page.trigger_calls, [])
        self.assertEqual(after_limit.json_calls, 0)
        self._assert_closed(page, context, browser, playwright)

    def test_exact_100_on_verified_terminal_page_reports_platform_end(self) -> None:
        """终页正好 100 条时，平台已结束必须优先于硬上限警告。"""

        response = FakeResponse(
            comment_payload(
                *(comment_row(f"terminal-{index}") for index in range(100))
            )
        )
        starter, page, context, browser, playwright = self._harness(
            ((response,),)
        )

        batch = self._collector(starter).collect(
            self.account, "work-7", frozenset(), limit=100
        )

        self.assertEqual(batch.accepted_count, 100)
        self.assertEqual(batch.stop_reason, "platform_end")
        self.assertEqual(batch.warning_code, "")
        self.assertEqual(batch.page_count, 1)
        self.assertEqual(page.trigger_calls, [])
        self._assert_closed(page, context, browser, playwright)

    def test_lower_safe_limit_is_internal_truncation_not_hard_limit_warning(self) -> None:
        """小批量测试不能伪报已触发“100 条”硬上限警告。"""

        first = FakeResponse(
            comment_payload(
                comment_row("comment-1"),
                comment_row("comment-2"),
                cursor="cursor-1",
                has_more=True,
            )
        )
        starter, page, context, browser, playwright = self._harness(((first,),))

        batch = self._collector(starter).collect(
            self.account, "work-7", frozenset(), limit=1
        )

        self.assertEqual(batch.accepted_count, 1)
        self.assertEqual(batch.stop_reason, "")
        self.assertEqual(batch.warning_code, "")
        self.assertEqual(page.trigger_calls, [])
        self._assert_closed(page, context, browser, playwright)

    def test_malformed_rows_count_individually_but_terminal_valid_row_survives(self) -> None:
        """坏记录应计入拒绝数，但不得让同页好记录一起丢失。"""

        response = FakeResponse(
            comment_payload(
                comment_row("good"),
                comment_row("bad-like", like_count=-1),
                comment_row("nested", parent_id="parent"),
            )
        )
        starter, page, context, browser, playwright = self._harness(((response,),))

        batch = self._collector(starter).collect(
            self.account, "work-7", frozenset()
        )

        self.assertEqual(batch.accepted_count, 1)
        self.assertEqual(batch.rejected_count, 1)
        self.assertEqual(batch.stop_reason, "platform_end")
        self._assert_closed(page, context, browser, playwright)

    def test_no_valid_row_without_platform_end_fails_with_fixed_error(self) -> None:
        """空的非终页不能触发翻页来掩盖平台合同变化。"""

        response = FakeResponse(
            comment_payload(
                comment_row("bad", like_count=-1),
                cursor="cursor-1",
                has_more=True,
            )
        )
        starter, page, context, browser, playwright = self._harness(((response,),))

        with self.assertRaises(CommentInsightFailure) as raised:
            self._collector(starter).collect(
                self.account, "work-7", frozenset()
            )

        self.assertEqual(raised.exception.error_code, "comment_payload_invalid")
        self.assertIsNone(raised.exception.__cause__)
        self._assert_closed(page, context, browser, playwright)

    def test_cursor_loop_and_conflicting_duplicate_across_pages_fail_closed(self) -> None:
        """游标环和跨页同 ID 冲突都不能静默重试或覆盖。"""

        cases = (
            (
                FakeResponse(
                    comment_payload(
                        comment_row("one"), cursor="loop", has_more=True
                    )
                ),
                FakeResponse(
                    comment_payload(
                        comment_row("two"), cursor="loop", has_more=True
                    )
                ),
            ),
            (
                FakeResponse(
                    comment_payload(
                        comment_row("same", like_count=1),
                        cursor="next",
                        has_more=True,
                    )
                ),
                FakeResponse(
                    comment_payload(comment_row("same", like_count=2))
                ),
            ),
        )
        for first, second in cases:
            with self.subTest(first_cursor=first.payload["data"]["cursor"]):
                starter, page, context, browser, playwright = self._harness(
                    ((first,), (second,))
                )
                with self.assertRaises(CommentInsightFailure) as raised:
                    self._collector(starter).collect(
                        self.account, "work-7", frozenset()
                    )
                self.assertEqual(
                    raised.exception.error_code, "comment_payload_invalid"
                )
                self._assert_closed(page, context, browser, playwright)

    def test_missing_verified_ui_pagination_control_fails_without_request_replay(self) -> None:
        """页面没有合同里的翻页控件时必须停止，不得合成评论请求。"""

        response = FakeResponse(
            comment_payload(
                comment_row("one"), cursor="next", has_more=True
            )
        )
        starter, page, context, browser, playwright = self._harness(
            ((response,),),
            locator_error=RuntimeError("private selector failure"),
        )

        with self.assertRaises(CommentInsightFailure) as raised:
            self._collector(starter).collect(
                self.account, "work-7", frozenset()
            )

        self.assertEqual(raised.exception.error_code, "comment_payload_invalid")
        self.assertEqual(str(raised.exception), "comment_payload_invalid")
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(page.locator_calls, ["verified-comment-load-more"])
        self._assert_closed(page, context, browser, playwright)

    def test_invalid_inputs_fail_before_browser_with_fixed_error(self) -> None:
        """布尔限额、错账号、空作品和非匿名键集不能进入浏览器。"""

        starts = 0

        def forbidden_factory() -> object:
            nonlocal starts
            starts += 1
            raise AssertionError("invalid input must not start browser")

        collector = DouyinCommentDataCollector(
            contract_loader=verified_contract,
            playwright_factory=forbidden_factory,
            total_timeout_seconds=0.1,
            observed_at_factory=lambda: OBSERVED_AT,
        )
        cases = (
            (self.account, "work-7", frozenset(), True),
            (self.account, "work-7", frozenset(), 0),
            (self.account, "work-7", frozenset(), 101),
            ({**self.account, "id": True}, "work-7", frozenset(), 100),
            ({**self.account, "type": 4}, "work-7", frozenset(), 100),
            (self.account, "", frozenset(), 100),
            (self.account, "work-7", set(), 100),
            (self.account, "work-7", frozenset({"raw-platform-id"}), 100),
        )
        for account, content_id, known_keys, limit in cases:
            with self.subTest(content_id=content_id, limit=limit):
                with self.assertRaises(CommentInsightFailure) as raised:
                    collector.collect(account, content_id, known_keys, limit)
                self.assertEqual(
                    raised.exception.error_code, "comment_payload_invalid"
                )
                self.assertEqual(
                    str(raised.exception), "comment_payload_invalid"
                )
        self.assertEqual(starts, 0)

    def test_oversized_requested_content_id_fails_before_browser(self) -> None:
        """请求参数的超长作品 ID 也必须在 strip、URL 编码和启动前拒绝。"""

        starts = 0

        def forbidden_factory() -> object:
            nonlocal starts
            starts += 1
            raise AssertionError("oversized input must not start browser")

        collector = DouyinCommentDataCollector(
            contract_loader=verified_contract,
            playwright_factory=forbidden_factory,
            total_timeout_seconds=0.1,
            observed_at_factory=lambda: OBSERVED_AT,
        )

        with self.assertRaises(CommentInsightFailure) as raised:
            collector.collect(
                self.account,
                "x" * 513,
                frozenset(),
            )

        self.assertEqual(
            raised.exception.error_code, "comment_payload_invalid"
        )
        self.assertEqual(starts, 0)

    def test_total_deadline_begins_before_contract_and_observation_work(self) -> None:
        """合同与观测时间工作必须消耗同一个公开总时限。"""

        for slow_stage in ("contract", "observed_at"):
            with self.subTest(slow_stage=slow_stage):
                starts = 0

                def contract_loader() -> DouyinCommentContract:
                    if slow_stage == "contract":
                        time.sleep(0.008)
                    return verified_contract()

                def observed_at_factory() -> str:
                    if slow_stage == "observed_at":
                        time.sleep(0.008)
                    return OBSERVED_AT

                def forbidden_factory() -> object:
                    nonlocal starts
                    starts += 1
                    raise AssertionError("expired call must not start browser")

                collector = DouyinCommentDataCollector(
                    contract_loader=contract_loader,
                    playwright_factory=forbidden_factory,
                    total_timeout_seconds=0.005,
                    observed_at_factory=observed_at_factory,
                )

                started_at = time.monotonic()
                with self.assertRaises(CommentInsightFailure) as raised:
                    collector.collect(self.account, "work-7", frozenset())
                elapsed = time.monotonic() - started_at

                self.assertLess(elapsed, 0.03)
                self.assertEqual(
                    raised.exception.error_code, "comment_sync_timeout"
                )
                self.assertEqual(str(raised.exception), "comment_sync_timeout")
                self.assertEqual(starts, 0)

    def test_oversized_contract_strings_fail_before_transform_or_browser(self) -> None:
        """64MB 合同值不得先正则、大小写转换或扫描再拒绝。"""

        cases = (
            ("comment_navigation_template", "/secret-contract-", "/{content_id}"),
            ("comment_response_path", "/secret-contract-", ""),
            ("comment_response_method", "secretcontract", ""),
            ("comment_pagination_trigger", "click:secret-contract-", ""),
            ("comment_list_field", "secret-contract.", "[]"),
        )
        target_size = 64 * 1024 * 1024
        for field, prefix, suffix in cases:
            with self.subTest(field=field):
                oversized = prefix + (
                    "x" * (target_size - len(prefix) - len(suffix))
                ) + suffix
                starts = 0
                contract = verified_contract(**{field: oversized})

                def forbidden_factory() -> object:
                    nonlocal starts
                    starts += 1
                    raise AssertionError("oversized contract must not start browser")

                collector = DouyinCommentDataCollector(
                    contract_loader=lambda contract=contract: contract,
                    playwright_factory=forbidden_factory,
                    total_timeout_seconds=0.005,
                    observed_at_factory=lambda: OBSERVED_AT,
                )

                started_at = time.monotonic()
                with self.assertRaises(CommentInsightFailure) as raised:
                    collector.collect(self.account, "work-7", frozenset())
                elapsed = time.monotonic() - started_at

                self.assertLess(elapsed, 0.03)
                self.assertEqual(
                    raised.exception.error_code,
                    "comment_content_unavailable",
                )
                self.assertEqual(
                    str(raised.exception), "comment_content_unavailable"
                )
                self.assertEqual(starts, 0)
                artifacts, _values = production_exception_artifacts(
                    raised.exception
                )
                self.assertNotIn("secret-contract-", artifacts)
                self.assertIsNone(raised.exception.__cause__)
                del collector, contract, oversized
                gc.collect()

    def test_oversized_page_and_response_metadata_are_bounded_and_private(self) -> None:
        """64MB 页面/响应 URL 或方法不得越过总时限或进入 JSON。"""

        target_size = 64 * 1024 * 1024
        for metadata_kind in ("page_url", "response_url", "response_method"):
            with self.subTest(metadata_kind=metadata_kind):
                oversized = "secret-runtime-metadata-" + (
                    "x" * (target_size - len("secret-runtime-metadata-"))
                )
                response = None
                if metadata_kind == "page_url":
                    response_pages: tuple[tuple[FakeResponse, ...], ...] = ()
                    page_options = {"final_url": oversized}
                else:
                    response = FakeResponse(
                        comment_payload(comment_row("must-not-read")),
                        url=(
                            oversized
                            if metadata_kind == "response_url"
                            else "https://creator.douyin.com/verified/comment/list"
                        ),
                        method=(
                            oversized
                            if metadata_kind == "response_method"
                            else "GET"
                        ),
                    )
                    response_pages = ((response,),)
                    page_options = {}
                starter, page, context, browser, playwright = self._harness(
                    response_pages, **page_options
                )

                started_at = time.monotonic()
                with self.assertRaises(CommentInsightFailure) as raised:
                    self._collector(starter, timeout=0.005).collect(
                        self.account, "work-7", frozenset()
                    )
                elapsed = time.monotonic() - started_at

                self.assertLess(elapsed, 0.03)
                self.assertEqual(
                    raised.exception.error_code, "comment_payload_invalid"
                )
                self.assertEqual(str(raised.exception), "comment_payload_invalid")
                self.assertTrue(raised.exception.cleanup_receipt.closed)
                self.assertEqual(
                    raised.exception.cleanup_receipt.alive_resource_count, 0
                )
                if response is not None:
                    self.assertEqual(response.json_calls, 0)
                self.assertEqual(page.locator_calls, [])
                self.assertEqual(page.trigger_calls, [])
                self._assert_closed(page, context, browser, playwright)
                artifacts, values = production_exception_artifacts(
                    raised.exception
                )
                self.assertNotIn("secret-runtime-metadata-", artifacts)
                if response is not None:
                    self.assertNotIn(response, values)
                del oversized, starter, page, context, browser, playwright
                gc.collect()

    def test_missing_manifest_fails_closed_without_starting_browser(self) -> None:
        """没有真实验证清单时不能猜路径、字段或选择器。"""

        starts = 0

        def missing_contract() -> DouyinCommentContract:
            raise CommentInsightFailure("comment_content_unavailable")

        def forbidden_factory() -> object:
            nonlocal starts
            starts += 1
            raise AssertionError("no contract, no browser")

        collector = DouyinCommentDataCollector(
            contract_loader=missing_contract,
            playwright_factory=forbidden_factory,
            total_timeout_seconds=0.1,
            observed_at_factory=lambda: OBSERVED_AT,
        )
        with self.assertRaises(CommentInsightFailure) as raised:
            collector.collect(self.account, "work-7", frozenset())
        self.assertEqual(
            raised.exception.error_code, "comment_content_unavailable"
        )
        self.assertEqual(starts, 0)

    def test_timeout_login_verification_and_access_map_to_fixed_codes_after_cleanup(self) -> None:
        """平台状态与等待失败只能对外暴露固定错误码。"""

        cases = (
            ("https://creator.douyin.com/login", "comment_login_required", False),
            (
                "https://creator.douyin.com/verification",
                "comment_verification_required",
                False,
            ),
            (
                "https://creator.douyin.com/forbidden",
                "comment_access_denied",
                False,
            ),
            (
                "https://creator.douyin.com/verified/content/work-7/comments",
                "comment_sync_timeout",
                True,
            ),
        )
        for final_url, expected_code, retryable in cases:
            with self.subTest(expected_code=expected_code):
                starter, page, context, browser, playwright = self._harness(
                    (), final_url=final_url
                )
                with self.assertRaises(CommentInsightFailure) as raised:
                    self._collector(starter, timeout=0.03).collect(
                        self.account, "work-7", frozenset()
                    )
                self.assertEqual(raised.exception.error_code, expected_code)
                self.assertEqual(raised.exception.retryable, retryable)
                self.assertEqual(str(raised.exception), expected_code)
                self.assertIsNone(raised.exception.__cause__)
                self._assert_closed(page, context, browser, playwright)

    def test_cleanup_failure_overrides_success_with_fixed_receipt_and_report(self) -> None:
        """短会话未完整关闭时不能返回刚采集的评论。"""

        response = FakeResponse(comment_payload(comment_row("private-raw-id")))
        starter, page, context, browser, playwright = self._harness(((response,),))
        context.close_error = RuntimeError(f"private cleanup {self.state_file}")
        reports: list[dict] = []

        with self.assertRaises(CommentInsightFailure) as raised:
            self._collector(starter).collect(
                self.account,
                "work-7",
                frozenset(),
                report=reports.append,
            )

        self.assertEqual(raised.exception.error_code, "comment_sync_cancelled")
        self.assertEqual(str(raised.exception), "comment_sync_cancelled")
        self.assertIsNone(raised.exception.__cause__)
        self.assertFalse(raised.exception.cleanup_receipt.closed)
        self.assertEqual(raised.exception.cleanup_receipt.alive_resource_count, 1)
        self._assert_closed(page, context, browser, playwright)
        encoded = repr((raised.exception, reports))
        self.assertNotIn("private-raw-id", encoded)
        self.assertNotIn(str(self.state_file), encoded)
        self.assertNotIn("private cleanup", encoded)

    def test_report_delivery_reuses_one_worker_and_drops_while_blocked(self) -> None:
        """报告回调不得每次新建线程，阻塞时也不得积压旧回调。"""

        report_workers: list[threading.Thread] = []

        def record_report(_payload: dict) -> None:
            report_workers.append(threading.current_thread())

        for index in range(3):
            response = FakeResponse(
                comment_payload(comment_row(f"report-{index}"))
            )
            starter, page, context, browser, playwright = self._harness(
                ((response,),)
            )
            batch = self._collector(starter).collect(
                self.account,
                "work-7",
                frozenset(),
                report=record_report,
            )
            self.assertTrue(batch.cleanup_receipt.closed)
            self._assert_closed(page, context, browser, playwright)

        blocker = threading.Event()
        blocked_started = threading.Event()
        blocked_calls = 0

        def blocked_report(_payload: dict) -> None:
            nonlocal blocked_calls
            report_workers.append(threading.current_thread())
            blocked_calls += 1
            blocked_started.set()
            blocker.wait()

        first_response = FakeResponse(
            comment_payload(comment_row("report-blocked"))
        )
        first_starter, first_page, first_context, first_browser, first_playwright = (
            self._harness(((first_response,),))
        )
        blocked_outcome: dict[str, object] = {}

        def run_blocked() -> None:
            blocked_outcome["batch"] = self._collector(
                first_starter, timeout=0.2
            ).collect(
                self.account,
                "work-7",
                frozenset(),
                report=blocked_report,
            )

        caller = threading.Thread(target=run_blocked, daemon=True)
        caller.start()
        self.assertTrue(blocked_started.wait(0.1))

        for index in range(4):
            response = FakeResponse(
                comment_payload(comment_row(f"report-dropped-{index}"))
            )
            starter, page, context, browser, playwright = self._harness(
                ((response,),)
            )
            batch = self._collector(starter).collect(
                self.account,
                "work-7",
                frozenset(),
                report=blocked_report,
            )
            self.assertTrue(batch.cleanup_receipt.closed)
            self._assert_closed(page, context, browser, playwright)

        self.assertEqual(blocked_calls, 1)
        self.assertLessEqual(
            sum(
                item.name == "douyin-comment-report"
                for item in threading.enumerate()
            ),
            1,
        )
        blocker.set()
        caller.join(0.3)
        self.assertFalse(caller.is_alive())
        self.assertTrue(blocked_outcome["batch"].cleanup_receipt.closed)
        self._assert_closed(
            first_page, first_context, first_browser, first_playwright
        )
        self.assertEqual(len({id(item) for item in report_workers}), 1)

    def test_fixed_failure_traceback_does_not_retain_raw_response_or_row(self) -> None:
        """固定错误的 traceback 不得持有评论 ID、正文、作者或响应对象。"""

        first = comment_row(
            "raw-secret-id",
            body="raw-secret-body",
            author={"uid": "raw-secret-uid", "nickname": "raw-secret-name"},
            like_count=1,
        )
        second = dict(first)
        second["like_count"] = 2
        payload = comment_payload(first, second)
        response = FakeResponse(payload)
        starter, page, context, browser, playwright = self._harness(((response,),))

        with self.assertRaises(CommentInsightFailure) as raised:
            self._collector(starter).collect(
                self.account, "work-7", frozenset()
            )

        encoded, values = production_exception_artifacts(raised.exception)
        self.assertEqual(raised.exception.error_code, "comment_payload_invalid")
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)
        for forbidden in (
            "raw-secret-id",
            "raw-secret-body",
            "raw-secret-uid",
            "raw-secret-name",
        ):
            self.assertNotIn(forbidden, encoded)
        self.assertNotIn(response, values)
        self.assertNotIn(payload, values)
        self.assertNotIn(first, values)
        self._assert_closed(page, context, browser, playwright)

    def test_response_process_control_is_recreated_without_raw_values(self) -> None:
        """响应中的进程控制异常只保留类型，不保留平台原始消息。"""

        for exception_type in (
            asyncio.CancelledError,
            KeyboardInterrupt,
            SystemExit,
        ):
            with self.subTest(exception=exception_type.__name__):
                response = FakeResponse(
                    comment_payload(comment_row()),
                    error=exception_type("raw-control-secret"),
                )
                starter, page, context, browser, playwright = self._harness(
                    ((response,),)
                )

                with self.assertRaises(exception_type) as raised:
                    self._collector(starter).collect(
                        self.account, "work-7", frozenset()
                    )

                encoded, values = production_exception_artifacts(
                    raised.exception
                )
                self.assertNotIn("raw-control-secret", encoded)
                self.assertEqual(raised.exception.args, ())
                self.assertNotIn(response, values)
                self.assertIsNone(raised.exception.__cause__)
                self.assertIsNone(raised.exception.__context__)
                self._assert_closed(page, context, browser, playwright)

    def test_cleanup_process_control_is_recreated_without_raw_values(self) -> None:
        """清理阶段的控制异常也不得带出底层消息。"""

        response = FakeResponse(comment_payload(comment_row()))
        starter, page, context, browser, playwright = self._harness(((response,),))
        page.close_error = KeyboardInterrupt("raw-cleanup-control")

        with self.assertRaises(KeyboardInterrupt) as raised:
            self._collector(starter).collect(
                self.account, "work-7", frozenset()
            )

        encoded, values = production_exception_artifacts(raised.exception)
        self.assertNotIn("raw-cleanup-control", encoded)
        self.assertEqual(raised.exception.args, ())
        self.assertNotIn(response, values)
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)
        self._assert_closed(page, context, browser, playwright)

    def test_total_deadline_bounds_start_through_all_four_closes(self) -> None:
        """各启动阶段和四层关闭必须共用一个总时限，不得分别重置。"""

        for stage in ("start", "launch", "context", "page", "goto"):
            with self.subTest(stage=stage):
                starter, page, context, browser, playwright = self._harness(())
                if stage == "start":
                    starter.start_delay = 0.06
                elif stage == "launch":
                    playwright.chromium.launch_delay = 0.06
                elif stage == "context":
                    browser.context_delay = 0.06
                elif stage == "page":
                    context.page_delay = 0.06
                else:
                    page.goto_delay = 0.06
                collector = self._collector(starter, timeout=0.02)

                started_at = time.monotonic()
                with self.assertRaises(CommentInsightFailure) as raised:
                    collector.collect(self.account, "work-7", frozenset())
                elapsed = time.monotonic() - started_at

                self.assertEqual(
                    raised.exception.error_code, "comment_sync_timeout"
                )
                self.assertLess(elapsed, 0.05)

        response = FakeResponse(comment_payload(comment_row()))
        starter, page, context, browser, playwright = self._harness(((response,),))
        page.close_delay = 0.05
        context.close_delay = 0.05
        browser.close_delay = 0.05
        playwright.stop_delay = 0.05
        collector = self._collector(starter, timeout=0.04)

        started_at = time.monotonic()
        with self.assertRaises(CommentInsightFailure) as raised:
            collector.collect(self.account, "work-7", frozenset())
        elapsed = time.monotonic() - started_at

        self.assertEqual(raised.exception.error_code, "comment_sync_cancelled")
        self.assertLess(elapsed, 0.08)
        self._assert_closed(page, context, browser, playwright)

    def test_cpu_heavy_50000_row_payload_obeys_total_deadline(self) -> None:
        """5 万行载荷不得在同步解析里越过总墙钟时限。"""

        rows = [
            comment_row(
                f"raw-heavy-{index}",
                parent_id="nested-parent",
                author={
                    "uid": f"raw-heavy-uid-{index}",
                    "nickname": "raw-heavy-name",
                },
            )
            for index in range(50_000)
        ]
        payload = comment_payload(
            *rows,
            cursor="heavy-cursor",
            has_more=True,
        )
        response = FakeResponse(payload)
        starter, page, context, browser, playwright = self._harness(((response,),))
        collector = self._collector(starter, timeout=0.001)

        outcome, elapsed, diagnostics, still_running = self._run_with_wall_limit(
            lambda: collector.collect(self.account, "work-7", frozenset()),
            wall_limit=0.12,
        )

        self.assertFalse(still_running)
        self.assertLess(elapsed, 0.12)
        error = outcome["error"]
        self.assertIsInstance(error, CommentInsightFailure)
        self.assertIn(
            error.error_code,
            {"comment_sync_timeout", "comment_payload_invalid"},
        )
        self.assertNotIn("raw-heavy", diagnostics)
        self._assert_closed(page, context, browser, playwright)

    def test_single_slow_row_projection_obeys_deadline_and_audits_worker(self) -> None:
        """单行投影在公开时限内不返回也必须有界并如实审计。"""

        # 其他测试可能刚刚完成受审 worker 任务；本例需要独立提交慢任务。
        time.sleep(0.08)
        response = FakeResponse(
            comment_payload(
                comment_row(
                    "raw-slow-projector-id",
                    body="raw-slow-projector-body",
                    author={
                        "uid": "raw-slow-projector-uid",
                        "nickname": "raw-slow-projector-name",
                    },
                )
            )
        )
        starter, page, context, browser, playwright = self._harness(
            ((response,),)
        )

        projection_started = threading.Event()
        release_projection = threading.Event()
        projection_finished = threading.Event()

        def slow_derive(*args) -> str:
            projection_started.set()
            release_projection.wait()
            try:
                return derive_comment_key(*args)
            finally:
                projection_finished.set()

        outcome: dict[str, object] = {}

        def invoke() -> None:
            try:
                outcome["value"] = self._collector(
                    starter, timeout=0.02
                ).collect(self.account, "work-7", frozenset())
            except BaseException as exc:
                outcome["error"] = exc

        diagnostics = io.StringIO()
        runner = threading.Thread(
            target=invoke,
            name="douyin-comment-bounded-call",
            daemon=True,
        )
        with patch(
            "app_core.douyin_comment_data_collector.derive_comment_key",
            side_effect=slow_derive,
        ):
            started_at = time.monotonic()
            with redirect_stderr(diagnostics):
                runner.start()
                projection_did_start = projection_started.wait(0.04)
                runner.join(0.05)
                elapsed = time.monotonic() - started_at
                still_running = runner.is_alive()
                release_projection.set()
                if projection_did_start:
                    projection_finished.wait(0.2)
                runner.join(0.1)
                gc.collect()

        self.assertTrue(projection_did_start)
        self.assertFalse(still_running)
        self.assertLess(elapsed, 0.05)
        error = outcome["error"]
        self.assertIsInstance(error, CommentInsightFailure)
        self.assertEqual(error.error_code, "comment_sync_cancelled")
        self.assertFalse(error.cleanup_receipt.closed)
        self.assertGreater(error.cleanup_receipt.alive_resource_count, 0)
        encoded, values = production_exception_artifacts(error)
        for forbidden in (
            "raw-slow-projector-id",
            "raw-slow-projector-body",
            "raw-slow-projector-uid",
            "raw-slow-projector-name",
        ):
            self.assertNotIn(forbidden, encoded)
            self.assertNotIn(
                forbidden,
                production_thread_artifacts("douyin-comment-projector"),
            )
        self.assertNotIn(response, values)
        self.assertEqual(diagnostics.getvalue(), "")
        self._assert_closed(page, context, browser, playwright)

    def test_repeated_slow_projections_use_one_bounded_worker(self) -> None:
        """重复慢投影不得累积 worker 或未审计存活任务。"""

        time.sleep(0.08)
        harnesses = tuple(
            self._harness(
                ((FakeResponse(comment_payload(comment_row(f"slow-{index}"))),),)
            )
            for index in range(4)
        )
        outcomes: list[dict[str, object]] = []
        still_running_values: list[bool] = []

        def slow_derive(*args) -> str:
            time.sleep(0.06)
            return derive_comment_key(*args)

        with patch(
            "app_core.douyin_comment_data_collector.derive_comment_key",
            side_effect=slow_derive,
        ):
            for starter, *_resources in harnesses:
                outcome, elapsed, diagnostics, still_running = (
                    self._run_with_wall_limit(
                        lambda starter=starter: self._collector(
                            starter, timeout=0.02
                        ).collect(self.account, "work-7", frozenset()),
                        wall_limit=0.05,
                    )
                )
                outcomes.append(outcome)
                still_running_values.append(still_running)
                self.assertLess(elapsed, 0.05)
                self.assertEqual(diagnostics, "")
                time.sleep(0.08)
            time.sleep(0.08)

        self.assertEqual(still_running_values, [False] * 4)
        self.assertEqual(
            sum(
                item.name == "douyin-comment-projector"
                for item in threading.enumerate()
            ),
            1,
        )
        for outcome, resources in zip(outcomes, harnesses):
            error = outcome["error"]
            self.assertIsInstance(error, CommentInsightFailure)
            self.assertIn(
                error.error_code,
                {"comment_sync_cancelled", "comment_sync_timeout"},
            )
            if error.error_code == "comment_sync_cancelled":
                self.assertFalse(error.cleanup_receipt.closed)
                self.assertGreater(
                    error.cleanup_receipt.alive_resource_count, 0
                )
            else:
                self.assertTrue(error.cleanup_receipt.closed)
                self.assertEqual(
                    error.cleanup_receipt.alive_resource_count, 0
                )
            self._assert_closed(*resources[1:])
        thread_artifacts = production_thread_artifacts(
            "douyin-comment-projector"
        )
        self.assertNotIn("slow-0", thread_artifacts)
        self.assertNotIn("private-default", thread_artifacts)

    def test_huge_builtin_comment_id_and_body_fail_before_heavy_work(self) -> None:
        """80MB 内建字符串必须先用长度上限拒绝，不得哈希或投影。"""

        for field in ("comment_id", "body"):
            with self.subTest(field=field):
                oversized = f"raw-oversized-{field}-" + (
                    "x" * (80 * 1024 * 1024)
                )
                row = comment_row(
                    oversized if field == "comment_id" else "small-id",
                    body=oversized if field == "body" else "small-body",
                    author={
                        "uid": "raw-oversized-author-uid",
                        "nickname": "raw-oversized-author-name",
                    },
                )
                response = FakeResponse(comment_payload(row))
                starter, page, context, browser, playwright = self._harness(
                    ((response,),)
                )

                outcome, elapsed, diagnostics, still_running = (
                    self._run_with_wall_limit(
                        lambda: self._collector(
                            starter, timeout=0.02
                        ).collect(self.account, "work-7", frozenset()),
                        wall_limit=0.05,
                    )
                )
                if still_running:
                    time.sleep(0.1)

                self.assertFalse(still_running)
                self.assertLess(elapsed, 0.05)
                error = outcome["error"]
                self.assertIsInstance(error, CommentInsightFailure)
                self.assertEqual(
                    error.error_code, "comment_payload_invalid"
                )
                self.assertTrue(error.cleanup_receipt.closed)
                encoded, values = production_exception_artifacts(error)
                self.assertNotIn(f"raw-oversized-{field}", encoded)
                self.assertNotIn(response, values)
                self.assertEqual(diagnostics, "")
                self._assert_closed(
                    page, context, browser, playwright
                )
                del oversized, row, response, outcome, error
                gc.collect()

    def test_matching_response_flood_hits_bounded_queue_without_backlog(self) -> None:
        """大量精确匹配响应不得在内存中形成无界原始响应队列。"""

        responses = tuple(
            FakeResponse(
                comment_payload(comment_row(f"flood-{index}")),
                delay=0.03,
            )
            for index in range(200)
        )
        starter, page, context, browser, playwright = self._harness(
            (responses,)
        )
        collector = self._collector(starter, timeout=0.05)

        outcome, elapsed, diagnostics, still_running = self._run_with_wall_limit(
            lambda: collector.collect(self.account, "work-7", frozenset()),
            wall_limit=0.18,
        )

        self.assertFalse(still_running)
        self.assertLess(elapsed, 0.18)
        error = outcome["error"]
        self.assertIsInstance(error, CommentInsightFailure)
        self.assertEqual(error.error_code, "comment_payload_invalid")
        self.assertLessEqual(sum(item.json_calls for item in responses), 1)
        self.assertNotIn("flood-199", diagnostics)
        self._assert_closed(page, context, browser, playwright)

    def _assert_late_creation_is_owned(self, stage: str) -> None:
        starter, page, context, browser, playwright = self._harness(())
        if stage == "playwright":
            starter.start_delay = 0.2
            starter.start_after_cancel_delay = 0.0
        elif stage == "browser":
            playwright.chromium.launch_delay = 0.2
            playwright.chromium.launch_after_cancel_delay = 0.0
        elif stage == "context":
            browser.context_delay = 0.2
            browser.context_after_cancel_delay = 0.0
        else:
            context.page_delay = 0.2
            context.page_after_cancel_delay = 0.0
        collector = self._collector(starter, timeout=0.08)

        with self.assertRaises(CommentInsightFailure) as raised:
            collector.collect(self.account, "work-7", frozenset())

        self.assertEqual(raised.exception.error_code, "comment_sync_timeout")
        self.assertTrue(raised.exception.cleanup_receipt.closed)
        expected = {
            "playwright": (0, 0, 0, 1),
            "browser": (0, 0, 1, 1),
            "context": (0, 1, 1, 1),
            "page": (1, 1, 1, 1),
        }[stage]
        self.assertEqual(
            (page.closed, context.closed, browser.closed, playwright.stopped),
            expected,
        )

    def test_late_playwright_start_result_is_owned_and_stopped(self) -> None:
        """启动吞掉取消后返回的 Playwright 仍属于本次调用。"""

        self._assert_late_creation_is_owned("playwright")

    def test_creation_result_settled_before_cleanup_is_still_owned(self) -> None:
        """创建任务在清理扫描前完成时，结果也不得丢失。"""

        starter, page, context, browser, playwright = self._harness(())
        starter.start_delay = 0.04
        starter.start_after_cancel_delay = 0.0

        with self.assertRaises(CommentInsightFailure) as raised:
            self._collector(starter, timeout=0.04).collect(
                self.account, "work-7", frozenset()
            )

        self.assertEqual(raised.exception.error_code, "comment_sync_timeout")
        self.assertTrue(raised.exception.cleanup_receipt.closed)
        self.assertEqual(playwright.stopped, 1)
        self.assertEqual(
            (page.closed, context.closed, browser.closed),
            (0, 0, 0),
        )

    def test_late_browser_launch_result_is_owned_and_closed(self) -> None:
        """启动浏览器超时后晚到的 browser 不能变成无主资源。"""

        self._assert_late_creation_is_owned("browser")

    def test_late_context_creation_result_is_owned_and_closed(self) -> None:
        """建上下文超时后晚到的 context 必须继续关闭。"""

        self._assert_late_creation_is_owned("context")

    def test_late_page_creation_result_is_owned_and_closed(self) -> None:
        """建页面超时后晚到的 page 必须继续关闭。"""

        self._assert_late_creation_is_owned("page")

    def test_unresolved_resource_creation_reports_nonzero_alive_receipt(self) -> None:
        """总时限内仍未解决的资源创建任务不得报 closed=true。"""

        starter, page, context, browser, playwright = self._harness(())
        starter.start_delay = 0.04
        starter.start_after_cancel_delay = 0.2
        collector = self._collector(starter, timeout=0.04)

        outcome, elapsed, diagnostics, still_running = self._run_with_wall_limit(
            lambda: collector.collect(self.account, "work-7", frozenset())
        )

        self.assertFalse(still_running)
        self.assertLess(elapsed, 0.18)
        error = outcome["error"]
        self.assertIsInstance(error, CommentInsightFailure)
        self.assertEqual(error.error_code, "comment_sync_cancelled")
        self.assertFalse(error.cleanup_receipt.closed)
        self.assertGreater(error.cleanup_receipt.alive_resource_count, 0)
        self.assertEqual(playwright.stopped, 0)
        self.assertEqual(diagnostics, "")

    def test_cancellation_resistant_close_and_worker_cannot_hang_or_leak(self) -> None:
        """第三方协程吞掉取消时，公开入口仍须有界且不累积线程或警告。"""

        initial_unrelated_threads = {
            id(item)
            for item in threading.enumerate()
            if item.name != "douyin-comment-projector"
        }
        for mode in ("close", "worker"):
            with self.subTest(mode=mode):
                response = FakeResponse(
                    comment_payload(comment_row()),
                    resist_cancellation=mode == "worker",
                )
                starter, page, context, browser, playwright = self._harness(
                    ((response,),),
                    final_url=(
                        "https://creator.douyin.com/login"
                        if mode == "worker"
                        else "https://creator.douyin.com/verified/content/work-7/comments"
                    ),
                    close_resists_cancellation=mode == "close",
                )
                collector = self._collector(starter, timeout=0.04)

                outcome, elapsed, diagnostics, still_running = self._run_with_wall_limit(
                    lambda collector=collector: collector.collect(
                        self.account, "work-7", frozenset()
                    )
                )

                self.assertFalse(still_running)
                self.assertLess(elapsed, 0.18)
                self.assertIsInstance(
                    outcome.get("error"), CommentInsightFailure
                )
                error = outcome["error"]
                self.assertEqual(error.error_code, "comment_sync_cancelled")
                self.assertEqual(str(error), "comment_sync_cancelled")
                self.assertIsNone(error.__cause__)
                self.assertEqual(page.listener_removals, 1)
                self._assert_closed(page, context, browser, playwright)
                self.assertEqual(diagnostics, "")
        self.assertEqual(
            {
                id(item)
                for item in threading.enumerate()
                if item.name != "douyin-comment-projector"
            },
            initial_unrelated_threads,
        )
        self.assertLessEqual(
            sum(
                item.name == "douyin-comment-projector"
                for item in threading.enumerate()
            ),
            1,
        )

    def test_process_control_from_navigation_and_cleanup_survives_all_closes(self) -> None:
        """CancelledError、KeyboardInterrupt 和 SystemExit 不能被业务错误覆盖。"""

        for stage in ("navigation", "cleanup"):
            for exception_type in (
                asyncio.CancelledError,
                KeyboardInterrupt,
                SystemExit,
            ):
                with self.subTest(stage=stage, exception=exception_type.__name__):
                    response = FakeResponse(comment_payload(comment_row()))
                    starter, page, context, browser, playwright = self._harness(
                        ((response,),)
                    )
                    if stage == "navigation":
                        page.goto_error = exception_type()
                    else:
                        page.close_error = exception_type()
                    collector = self._collector(starter, timeout=0.1)

                    with self.assertRaises(exception_type):
                        collector.collect(self.account, "work-7", frozenset())

                    self._assert_closed(page, context, browser, playwright)

    def test_late_results_from_shared_collector_stay_account_and_content_isolated(self) -> None:
        """并发的旧账号慢响应不能覆盖新账号或其他作品。"""

        second_state = self.cookie_dir / "oneclick_3_second.json"
        second_state.write_text(
            json.dumps({"cookies": [], "origins": []}), encoding="utf-8"
        )
        second_account = {"id": 13, "type": 3, "filePath": second_state.name}
        first_starter, *_ = self._harness(
            ((FakeResponse(comment_payload(comment_row("first")), delay=0.03),),),
            final_url="https://creator.douyin.com/verified/content/work-7/comments",
        )
        second_starter, *_ = self._harness(
            ((FakeResponse(comment_payload(comment_row("second", content_id="work-8"))),),),
            final_url="https://creator.douyin.com/verified/content/work-8/comments",
        )
        starters = {"account-12": first_starter, "account-13": second_starter}
        collector = DouyinCommentDataCollector(
            contract_loader=verified_contract,
            playwright_factory=lambda: starters[threading.current_thread().name],
            total_timeout_seconds=0.2,
            observed_at_factory=lambda: OBSERVED_AT,
        )
        results: dict[int, object] = {}
        errors: list[BaseException] = []

        def run(account: dict, content_id: str) -> None:
            try:
                results[account["id"]] = collector.collect(
                    account, content_id, frozenset()
                )
            except BaseException as exc:
                errors.append(exc)

        first = threading.Thread(
            target=run, args=(self.account, "work-7"), name="account-12"
        )
        second = threading.Thread(
            target=run, args=(second_account, "work-8"), name="account-13"
        )
        first.start()
        second.start()
        first.join(1)
        second.join(1)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[12].content_id, "work-7")
        self.assertEqual(results[13].content_id, "work-8")
        self.assertEqual(
            results[12].comments[0].comment_key,
            derive_comment_key(12, "work-7", "first"),
        )
        self.assertEqual(
            results[13].comments[0].comment_key,
            derive_comment_key(13, "work-8", "second"),
        )

    def test_late_wrong_content_response_from_reused_page_cannot_contaminate_next_call(self) -> None:
        """同一页面的旧监听响应不得进入下一次作品采集。"""

        first_response = FakeResponse(
            comment_payload(comment_row("first-work-7"))
        )
        starter, page, context, browser, playwright = self._harness(
            ((first_response,),)
        )
        collector = self._collector(starter, timeout=0.2)

        first_batch = collector.collect(
            self.account, "work-7", frozenset()
        )
        old_listener = page.listener_history[0]
        self.assertEqual(first_batch.content_id, "work-7")

        second_response = FakeResponse(
            comment_payload(comment_row("second-work-8", content_id="work-8")),
            delay=0.03,
        )
        page.response_pages = ((second_response,),)
        page.url = (
            "https://creator.douyin.com/verified/content/work-8/comments"
        )
        page.next_page_index = 0
        outcome: dict[str, object] = {}

        def collect_second() -> None:
            try:
                outcome["batch"] = collector.collect(
                    self.account, "work-8", frozenset()
                )
            except BaseException as exc:
                outcome["error"] = exc

        runner = threading.Thread(target=collect_second, daemon=True)
        runner.start()
        deadline = time.monotonic() + 0.1
        while len(page.listener_history) < 2 and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(len(page.listener_history), 2)

        late_old_response = FakeResponse(
            comment_payload(comment_row("late-work-7", content_id="work-7"))
        )
        old_listener(late_old_response)
        runner.join(0.3)

        self.assertFalse(runner.is_alive())
        self.assertNotIn("error", outcome)
        second_batch = outcome["batch"]
        self.assertEqual(second_batch.content_id, "work-8")
        self.assertEqual(second_batch.accepted_count, 1)
        self.assertEqual(late_old_response.json_calls, 0)
        self.assertEqual(
            (page.closed, context.closed, browser.closed, playwright.stopped),
            (2, 2, 2, 2),
        )


if __name__ == "__main__":
    unittest.main()
