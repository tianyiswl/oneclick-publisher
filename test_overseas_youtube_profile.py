# -*- coding: utf-8 -*-
"""YouTube OAuth 公开频道资料和安全头像下载合同。"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from app_core.overseas_youtube_api import YouTubeChannelIdentity
from app_core.overseas_youtube_profile import (
    YouTubeAvatarDownloader,
    YouTubeProfileError,
    refresh_youtube_oauth_profile,
    youtube_studio_url,
)


def valid_png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (4, 4), color=(32, 64, 96)).save(output, format="PNG")
    return output.getvalue()


class FakeAvatarResponse:
    def __init__(
        self,
        status_code: int,
        body: bytes = b"",
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = dict(headers or {})
        self._body = bytes(body)
        self.closed = False

    def iter_content(self, chunk_size: int):
        for start in range(0, len(self._body), max(1, int(chunk_size))):
            yield self._body[start : start + chunk_size]

    def close(self) -> None:
        self.closed = True


class FakeAvatarTransport:
    def __init__(self, *responses: FakeAvatarResponse) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def get(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected avatar request")
        return self.responses.pop(0)


class RecordingDownloader:
    def __init__(self, file_name: str = "oneclick_account_7.png") -> None:
        self.file_name = file_name
        self.calls: list[dict[str, object]] = []

    def download(self, url: str, *, account_id: int, avatar_dir: Path) -> str:
        self.calls.append(
            {"url": url, "account_id": account_id, "avatar_dir": avatar_dir}
        )
        return self.file_name


class FailingDownloader:
    def download(self, url: str, *, account_id: int, avatar_dir: Path) -> str:
        raise YouTubeProfileError("youtube_avatar_download_failed")


def youtube_account(account_id: int = 7) -> dict[str, object]:
    return {
        "id": account_id,
        "type": 7,
        "authMode": "youtube_oauth",
        "filePath": "youtube-oauth:opaque-reference",
        "accountReference": "UC_safe",
        "userName": "Old Channel",
        "avatarPath": "old.png",
    }


class YouTubeProfileTests(unittest.TestCase):
    def test_studio_url_uses_saved_public_channel_id(self) -> None:
        self.assertEqual(
            youtube_studio_url({"accountReference": "UC_safe"}),
            "https://studio.youtube.com/channel/UC_safe",
        )
        self.assertEqual(
            youtube_studio_url({"accountReference": "https://attacker.test"}),
            "https://studio.youtube.com/",
        )

    def test_avatar_downloader_rejects_unapproved_host_before_request(self) -> None:
        transport = FakeAvatarTransport()
        downloader = YouTubeAvatarDownloader(transport)

        with self.assertRaisesRegex(
            YouTubeProfileError, "^youtube_avatar_url_invalid$"
        ):
            downloader.download("https://127.0.0.1/avatar.png", account_id=7)

        self.assertEqual(transport.calls, [])

    def test_avatar_redirect_is_revalidated_before_following(self) -> None:
        response = FakeAvatarResponse(
            302,
            headers={"Location": "https://attacker.test/collect"},
        )
        transport = FakeAvatarTransport(response)
        downloader = YouTubeAvatarDownloader(transport)

        with self.assertRaisesRegex(
            YouTubeProfileError, "^youtube_avatar_url_invalid$"
        ):
            downloader.download("https://yt3.ggpht.com/avatar", account_id=7)

        self.assertEqual(len(transport.calls), 1)
        self.assertTrue(response.closed)

    def test_download_decodes_and_atomically_replaces_the_old_avatar(self) -> None:
        transport = FakeAvatarTransport(FakeAvatarResponse(200, valid_png_bytes()))
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            destination = root / "oneclick_account_7.png"
            destination.write_bytes(b"old-avatar")

            name = YouTubeAvatarDownloader(transport).download(
                "https://yt3.ggpht.com/avatar",
                account_id=7,
                avatar_dir=root,
            )

            self.assertEqual(name, "oneclick_account_7.png")
            with Image.open(destination) as image:
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.size, (4, 4))
            self.assertFalse((root / ".oneclick_account_7.png.tmp").exists())
        self.assertIs(transport.calls[0]["allow_redirects"], False)
        self.assertIs(transport.calls[0]["stream"], True)

    def test_decode_failure_preserves_the_previous_avatar(self) -> None:
        transport = FakeAvatarTransport(FakeAvatarResponse(200, b"not-an-image"))
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            destination = root / "oneclick_account_7.png"
            destination.write_bytes(b"old-avatar")

            with self.assertRaisesRegex(
                YouTubeProfileError, "^youtube_avatar_invalid$"
            ):
                YouTubeAvatarDownloader(transport).download(
                    "https://yt3.ggpht.com/avatar",
                    account_id=7,
                    avatar_dir=root,
                )

            self.assertEqual(destination.read_bytes(), b"old-avatar")
            self.assertFalse((root / ".oneclick_account_7.png.tmp").exists())

    def test_refresh_validates_same_channel_and_downloads_official_avatar(self) -> None:
        downloader = RecordingDownloader()
        with tempfile.TemporaryDirectory() as raw, patch(
            "app_core.overseas_youtube_profile.validate_saved_youtube_oauth_account",
            return_value=YouTubeChannelIdentity(
                "UC_safe",
                "Safe Channel",
                "https://yt3.ggpht.com/avatar?private-query=discarded",
            ),
        ):
            result = refresh_youtube_oauth_profile(
                youtube_account(),
                avatar_dir=Path(raw),
                downloader=downloader,
            )

        self.assertEqual(result["displayName"], "Safe Channel")
        self.assertEqual(result["avatarFileName"], "oneclick_account_7.png")
        self.assertEqual(downloader.calls[0]["account_id"], 7)
        self.assertNotIn("private-query", repr(result))

    def test_refresh_rejects_a_different_channel_before_downloading(self) -> None:
        downloader = RecordingDownloader()
        with tempfile.TemporaryDirectory() as raw, patch(
            "app_core.overseas_youtube_profile.validate_saved_youtube_oauth_account",
            return_value=YouTubeChannelIdentity(
                "UC_other",
                "Other Channel",
                "https://yt3.ggpht.com/avatar",
            ),
        ):
            with self.assertRaisesRegex(
                YouTubeProfileError, "^youtube_channel_identity_mismatch$"
            ):
                refresh_youtube_oauth_profile(
                    youtube_account(),
                    avatar_dir=Path(raw),
                    downloader=downloader,
                )

        self.assertEqual(downloader.calls, [])

    def test_refresh_normalizes_remote_avatar_failure_to_stable_public_code(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch(
            "app_core.overseas_youtube_profile.validate_saved_youtube_oauth_account",
            return_value=YouTubeChannelIdentity(
                "UC_safe",
                "Safe Channel",
                "https://yt3.ggpht.com/avatar",
            ),
        ):
            with self.assertRaisesRegex(
                YouTubeProfileError, "^youtube_avatar_fetch_failed$"
            ):
                refresh_youtube_oauth_profile(
                    youtube_account(),
                    avatar_dir=Path(raw),
                    downloader=FailingDownloader(),
                )

    def test_refresh_missing_remote_avatar_uses_stable_public_code(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch(
            "app_core.overseas_youtube_profile.validate_saved_youtube_oauth_account",
            return_value=YouTubeChannelIdentity("UC_safe", "Safe Channel", None),
        ):
            with self.assertRaisesRegex(
                YouTubeProfileError, "^youtube_avatar_fetch_failed$"
            ):
                refresh_youtube_oauth_profile(
                    youtube_account(),
                    avatar_dir=Path(raw),
                    downloader=RecordingDownloader(),
                )


if __name__ == "__main__":
    unittest.main()
