# -*- coding: utf-8 -*-
"""TikTok 已验证账号的公开昵称和头像回读测试。"""

from __future__ import annotations

import asyncio
import base64
import importlib
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image

from app_core.overseas_tiktok_identity import TikTokIdentity, TikTokIdentityError


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _profile_service():
    module_name = "app_core.overseas_tiktok_profile"
    if importlib.util.find_spec(module_name) is None:
        raise AssertionError("TikTok 公开资料读取模块尚未实现")
    return importlib.import_module(module_name)


class _FakeAvatar:
    def __init__(
        self,
        image: bytes = PNG_1X1,
        *,
        visible_samples: list[bool] | None = None,
    ) -> None:
        self.first = self
        self.image = image
        self.visible_samples = list(visible_samples or [True])
        self.visibility_calls = 0
        self.screenshot_calls = 0

    async def count(self) -> int:
        return 1

    async def is_visible(self, **_kwargs) -> bool:
        index = min(self.visibility_calls, len(self.visible_samples) - 1)
        self.visibility_calls += 1
        return self.visible_samples[index]

    async def screenshot(self, **_kwargs) -> bytes:
        self.screenshot_calls += 1
        return self.image


class _FakeProfilePage:
    def __init__(self, payloads: list[dict], avatar: _FakeAvatar | None = None) -> None:
        self.url = "https://www.tiktok.com/"
        self.payloads = list(payloads)
        self.avatar = avatar or _FakeAvatar()
        self.goto_calls: list[tuple[str, str, int]] = []
        self.evaluate_calls = 0
        self.locator_calls: list[str] = []

    async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.url = url
        self.goto_calls.append((url, wait_until, timeout))

    async def evaluate(self, _expression: str):
        index = min(self.evaluate_calls, len(self.payloads) - 1)
        self.evaluate_calls += 1
        return self.payloads[index]

    def locator(self, selector: str):
        self.locator_calls.append(selector)
        return self.avatar


class _FakeHttpResponse:
    def __init__(self, status_code: int, body: bytes, *, content_type: str) -> None:
        self.status_code = status_code
        self.body = body
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
        }
        self.closed = False

    def iter_content(self, chunk_size: int):
        for start in range(0, len(self.body), max(1, int(chunk_size))):
            yield self.body[start : start + chunk_size]

    def close(self) -> None:
        self.closed = True


class _FakeTransport:
    def __init__(self, responses: list[_FakeHttpResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs):
        self.calls.append((url, dict(kwargs)))
        return self.responses.pop(0)


class TikTokPublicProfileReaderTests(unittest.TestCase):
    def test_reader_uses_the_exact_verified_profile_for_name_and_avatar(self) -> None:
        service = _profile_service()
        payload = {
            "state": "ok",
            "uniqueId": "tianyiswl",
            "nickname": "Mobai",
        }
        page = _FakeProfilePage([payload, payload])
        identity = TikTokIdentity(
            "tianyiswl",
            "",
            "https://www.tiktok.com/@tianyiswl",
        )

        profile = asyncio.run(
            service.read_tiktok_public_profile(
                page,
                identity,
                poll_seconds=0.0,
                max_attempts=2,
            )
        )

        self.assertEqual(profile.handle, "tianyiswl")
        self.assertEqual(profile.display_name, "Mobai")
        self.assertTrue(profile.avatar_png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(
            page.goto_calls,
            [("https://www.tiktok.com/@tianyiswl", "domcontentloaded", 120_000)],
        )
        self.assertEqual(page.evaluate_calls, 2)
        self.assertEqual(page.avatar.screenshot_calls, 1)

    def test_reader_rejects_a_different_profile_before_capturing_avatar(self) -> None:
        service = _profile_service()
        payload = {
            "state": "ok",
            "uniqueId": "another.user",
            "nickname": "Another",
        }
        page = _FakeProfilePage([payload])

        with self.assertRaises(TikTokIdentityError) as raised:
            asyncio.run(
                service.read_tiktok_public_profile(
                    page,
                    TikTokIdentity(
                        "tianyiswl",
                        "",
                        "https://www.tiktok.com/@tianyiswl",
                    ),
                    poll_seconds=0.0,
                    max_attempts=2,
                )
            )

        self.assertEqual(
            raised.exception.error_code,
            "tiktok_account_identity_mismatch",
        )
        self.assertEqual(page.avatar.screenshot_calls, 0)

    def test_reader_waits_when_name_arrives_before_the_avatar_image(self) -> None:
        service = _profile_service()
        payload = {
            "state": "ok",
            "uniqueId": "tianyiswl",
            "nickname": "Mobai",
        }
        avatar = _FakeAvatar(visible_samples=[False, True])
        page = _FakeProfilePage([payload, payload, payload], avatar=avatar)

        profile = asyncio.run(
            service.read_tiktok_public_profile(
                page,
                TikTokIdentity(
                    "tianyiswl",
                    "",
                    "https://www.tiktok.com/@tianyiswl",
                ),
                poll_seconds=0.0,
                max_attempts=3,
            )
        )

        self.assertEqual(profile.display_name, "Mobai")
        self.assertEqual(avatar.visibility_calls, 2)
        self.assertEqual(avatar.screenshot_calls, 1)

    @staticmethod
    def _public_html(*, handle: str, nickname: str, avatar_url: str) -> bytes:
        payload = {
            "__DEFAULT_SCOPE__": {
                "webapp.user-detail": {
                    "userInfo": {
                        "user": {
                            "uniqueId": handle,
                            "nickname": nickname,
                            "avatarThumb": avatar_url,
                        }
                    }
                }
            }
        }
        return (
            '<html><script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" '
            f'type="application/json">{json.dumps(payload)}</script></html>'
        ).encode("utf-8")

    def test_public_fetch_reads_the_same_handle_without_using_a_browser_session(self) -> None:
        service = _profile_service()
        fetcher = getattr(service, "fetch_tiktok_public_profile", None)
        self.assertIsNotNone(fetcher, "公开主页资料回读尚未实现")
        avatar_url = "https://p19-common-sign.tiktokcdn-us.com/avatar.jpeg"
        transport = _FakeTransport(
            [
                _FakeHttpResponse(
                    200,
                    self._public_html(
                        handle="tianyiswl",
                        nickname="Mobai",
                        avatar_url=avatar_url,
                    ),
                    content_type="text/html; charset=utf-8",
                ),
                _FakeHttpResponse(200, PNG_1X1, content_type="image/png"),
            ]
        )

        profile = fetcher(
            TikTokIdentity(
                "tianyiswl",
                "",
                "https://www.tiktok.com/@tianyiswl",
            ),
            transport=transport,
        )

        self.assertEqual(profile.handle, "tianyiswl")
        self.assertEqual(profile.display_name, "Mobai")
        self.assertTrue(profile.avatar_png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual([call[0] for call in transport.calls], [
            "https://www.tiktok.com/@tianyiswl",
            avatar_url,
        ])
        self.assertTrue(all(response.closed for response in [
            *transport.responses,
        ]) or not transport.responses)

    def test_public_fetch_rejects_an_untrusted_avatar_url_before_requesting_it(self) -> None:
        service = _profile_service()
        fetcher = getattr(service, "fetch_tiktok_public_profile", None)
        self.assertIsNotNone(fetcher, "公开主页资料回读尚未实现")
        transport = _FakeTransport(
            [
                _FakeHttpResponse(
                    200,
                    self._public_html(
                        handle="tianyiswl",
                        nickname="Mobai",
                        avatar_url="http://127.0.0.1/private-avatar",
                    ),
                    content_type="text/html",
                )
            ]
        )

        with self.assertRaises(TikTokIdentityError) as raised:
            fetcher(
                TikTokIdentity(
                    "tianyiswl",
                    "",
                    "https://www.tiktok.com/@tianyiswl",
                ),
                transport=transport,
            )

        self.assertEqual(raised.exception.error_code, "tiktok_profile_unavailable")
        self.assertEqual(len(transport.calls), 1)


class TikTokPublicProfilePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "accounts.db"
        self.avatar_dir = self.root / "avatars"
        connection = sqlite3.connect(self.database)
        connection.execute(
            """
            CREATE TABLE user_info (
                id INTEGER PRIMARY KEY,
                type INTEGER NOT NULL,
                userName TEXT NOT NULL,
                accountReference TEXT,
                avatarPath TEXT,
                avatarUpdatedAt TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO user_info
                (id, type, userName, accountReference, avatarPath, avatarUpdatedAt)
            VALUES (13, 6, '@tianyiswl', 'tianyiswl', '', NULL)
            """
        )
        connection.commit()
        connection.close()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _stored(self) -> tuple:
        connection = sqlite3.connect(self.database)
        row = connection.execute(
            "SELECT userName, avatarPath, avatarUpdatedAt FROM user_info WHERE id = 13"
        ).fetchone()
        connection.close()
        return row

    def test_persist_updates_only_the_same_bound_account_and_writes_a_png(self) -> None:
        service = _profile_service()
        profile = service.TikTokPublicProfile(
            handle="tianyiswl",
            display_name="Mobai",
            avatar_png=PNG_1X1,
        )

        with patch.object(service, "connect", self._connect):
            file_name = service.persist_tiktok_public_profile(
                13,
                profile,
                avatar_dir=self.avatar_dir,
            )

        self.assertEqual(file_name, "oneclick_account_13.png")
        user_name, avatar_path, updated_at = self._stored()
        self.assertEqual(user_name, "Mobai")
        self.assertEqual(avatar_path, file_name)
        self.assertTrue(updated_at)
        with Image.open(self.avatar_dir / file_name) as image:
            self.assertEqual(image.format, "PNG")

    def test_persist_rejects_a_profile_for_another_bound_handle(self) -> None:
        service = _profile_service()
        profile = service.TikTokPublicProfile(
            handle="another.user",
            display_name="Another",
            avatar_png=PNG_1X1,
        )

        with (
            patch.object(service, "connect", self._connect),
            self.assertRaises(TikTokIdentityError) as raised,
        ):
            service.persist_tiktok_public_profile(
                13,
                profile,
                avatar_dir=self.avatar_dir,
            )

        self.assertEqual(
            raised.exception.error_code,
            "tiktok_account_identity_mismatch",
        )
        self.assertEqual(self._stored()[:2], ("@tianyiswl", ""))
        self.assertFalse(self.avatar_dir.exists())

    def test_persist_preserves_existing_avatar_when_compare_and_swap_rejects_update(self) -> None:
        service = _profile_service()
        profile = service.TikTokPublicProfile(
            handle="tianyiswl",
            display_name="Mobai",
            avatar_png=PNG_1X1,
        )
        destination = self.avatar_dir / "oneclick_account_13.png"
        self.avatar_dir.mkdir()
        destination.write_bytes(b"old-avatar")

        @contextmanager
        def concurrent_connect():
            connection = sqlite3.connect(self.database)
            connection.row_factory = sqlite3.Row

            class ConnectionWithConcurrentChange:
                def execute(self, query, parameters=()):
                    if "UPDATE user_info" in query and "SET userName" in query:
                        connection.execute(
                            "UPDATE user_info SET accountReference = ? WHERE id = ?",
                            ("concurrent.user", 13),
                        )
                    return connection.execute(query, parameters)

                def commit(self):
                    connection.commit()

            try:
                with connection:
                    yield ConnectionWithConcurrentChange()
            finally:
                connection.close()

        with (
            patch.object(service, "connect", concurrent_connect),
            self.assertRaises(TikTokIdentityError) as raised,
        ):
            service.persist_tiktok_public_profile(
                13,
                profile,
                avatar_dir=self.avatar_dir,
            )

        self.assertEqual(raised.exception.error_code, "tiktok_account_invalid")
        self.assertEqual(destination.read_bytes(), b"old-avatar")


if __name__ == "__main__":
    unittest.main()
