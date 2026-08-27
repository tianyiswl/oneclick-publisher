# -*- coding: utf-8 -*-
"""Offline contracts for official YouTube status and thumbnail management."""

from __future__ import annotations

import base64
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app_core.overseas_youtube_api import YOUTUBE_VIDEOS_ENDPOINT
from app_core.overseas_youtube_publish import (
    YOUTUBE_THUMBNAILS_UPLOAD_ENDPOINT,
    YouTubeOfficialPublishError,
    YouTubeVideoManagementClient,
    validate_youtube_publish_payload,
)


VALID_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUB"
    "AScY42YAAAAASUVORK5CYII="
)


class FakeResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


def video_status_response(
    visibility: str,
    publish_at: str | None = None,
    *,
    video_id: str = "video-1",
) -> dict[str, object]:
    status: dict[str, object] = {
        "privacyStatus": visibility,
        "selfDeclaredMadeForKids": False,
        "uploadStatus": "processed",
        "license": "youtube",
        "embeddable": True,
        "publicStatsViewable": True,
    }
    if publish_at is not None:
        status["publishAt"] = publish_at
    return {
        "items": [
            {
                "id": video_id,
                "snippet": {"channelId": "UC-test", "title": "测试视频"},
                "status": status,
                "processingDetails": {"processingStatus": "succeeded"},
            }
        ]
    }


class FakeManagementTransport:
    def __init__(
        self,
        *,
        get_responses: list[FakeResponse] | None = None,
        put_responses: list[FakeResponse] | None = None,
        post_responses: list[FakeResponse] | None = None,
    ) -> None:
        self.get_responses = list(get_responses or [])
        self.put_responses = list(put_responses or [])
        self.post_responses = list(post_responses or [])
        self.get_calls: list[dict[str, Any]] = []
        self.put_calls: list[dict[str, Any]] = []
        self.post_calls: list[dict[str, Any]] = []

    @staticmethod
    def _next(responses: list[FakeResponse]) -> FakeResponse:
        if not responses:
            raise AssertionError("unexpected management transport call")
        return responses.pop(0)

    def get(self, url: str, **kwargs) -> FakeResponse:
        self.get_calls.append({"url": url, **kwargs})
        return self._next(self.get_responses)

    def put(self, url: str, **kwargs) -> FakeResponse:
        self.put_calls.append({"url": url, **kwargs})
        return self._next(self.put_responses)

    def post(self, url: str, **kwargs) -> FakeResponse:
        self.post_calls.append({"url": url, **kwargs})
        return self._next(self.post_responses)


class YouTubePublishValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.video = self.root / "video.mp4"
        self.video.write_bytes(b"video")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _payload(self, **changes: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "type": 7,
            "youtubeOfficialApi": True,
            "fileList": [str(self.video)],
            "title": "测试标题",
            "description": "测试正文",
            "tags": ["oneclick"],
            "accountIds": [71],
            "accountList": ["youtube-oauth:test-reference"],
            "youtubeExpectedChannelId": "UC-test",
            "visibility": "private",
            "madeForKids": False,
            "notifySubscribers": True,
            "enableTimer": False,
            "scheduleTime": None,
            "scheduleTimezone": "Asia/Shanghai",
            "coverPath": "",
        }
        payload.update(changes)
        return payload

    def test_scheduled_public_converts_beijing_time_to_utc(self) -> None:
        checked = validate_youtube_publish_payload(
            self._payload(
                visibility="scheduled_public",
                enableTimer=True,
                scheduleTime="2026-08-28 09:00",
                madeForKids=False,
            ),
            now=datetime(
                2026,
                8,
                27,
                16,
                0,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ),
        )

        self.assertEqual(checked.settings.publish_at, "2026-08-28T01:00:00Z")
        self.assertEqual(checked.private_upload.visibility, "private")
        self.assertEqual(checked.expected_channel_id, "UC-test")

    def test_custom_thumbnail_over_two_megabytes_is_rejected(self) -> None:
        cover = self.root / "cover.png"
        cover.write_bytes(b"x" * (2 * 1024 * 1024 + 1))

        with self.assertRaisesRegex(
            YouTubeOfficialPublishError,
            "^youtube_thumbnail_invalid$",
        ):
            validate_youtube_publish_payload(
                self._payload(coverPath=str(cover))
            )

    def test_valid_thumbnail_and_explicit_audience_are_retained(self) -> None:
        cover = self.root / "cover.png"
        cover.write_bytes(VALID_PNG)

        checked = validate_youtube_publish_payload(
            self._payload(
                coverPath=str(cover),
                madeForKids=True,
                notifySubscribers=False,
                visibility="unlisted",
            )
        )

        self.assertEqual(checked.settings.thumbnail_path, cover.resolve())
        self.assertIs(checked.settings.made_for_kids, True)
        self.assertIs(checked.settings.notify_subscribers, False)

    def test_missing_explicit_audience_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            YouTubeOfficialPublishError,
            "^youtube_audience_required$",
        ):
            validate_youtube_publish_payload(self._payload(madeForKids=None))


class YouTubeVideoManagementClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.cover = Path(self.temp.name) / "cover.png"
        self.cover.write_bytes(VALID_PNG)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_scheduled_update_targets_exact_video_id(self) -> None:
        transport = FakeManagementTransport(
            get_responses=[
                FakeResponse(200, video_status_response("private")),
            ],
            put_responses=[
                FakeResponse(
                    200,
                    video_status_response(
                        "private", "2026-08-28T01:00:00Z"
                    ),
                )
            ],
        )
        client = YouTubeVideoManagementClient(transport)

        result = client.apply_visibility(
            "access-secret",
            video_id="video-1",
            target_visibility="scheduled_public",
            publish_at="2026-08-28T01:00:00Z",
            made_for_kids=False,
        )

        body = transport.put_calls[0]["json"]
        self.assertEqual(transport.get_calls[0]["url"], YOUTUBE_VIDEOS_ENDPOINT)
        self.assertEqual(body["id"], "video-1")
        self.assertEqual(body["status"]["privacyStatus"], "private")
        self.assertEqual(
            body["status"]["publishAt"], "2026-08-28T01:00:00Z"
        )
        self.assertEqual(body["status"]["license"], "youtube")
        self.assertEqual(result.publish_at, "2026-08-28T01:00:00Z")

    def test_thumbnail_posts_media_to_exact_video_id(self) -> None:
        transport = FakeManagementTransport(
            post_responses=[FakeResponse(200, {})]
        )
        client = YouTubeVideoManagementClient(transport)

        client.set_thumbnail(
            "access-secret",
            video_id="video-1",
            path=self.cover,
        )

        call = transport.post_calls[0]
        self.assertEqual(call["url"], YOUTUBE_THUMBNAILS_UPLOAD_ENDPOINT)
        self.assertEqual(
            call["params"],
            {"videoId": "video-1", "uploadType": "media"},
        )
        self.assertNotIn("access-secret", repr(client))

    def test_read_status_rejects_a_different_returned_video_id(self) -> None:
        transport = FakeManagementTransport(
            get_responses=[
                FakeResponse(
                    200,
                    video_status_response("private", video_id="different-video"),
                )
            ]
        )

        with self.assertRaisesRegex(
            YouTubeOfficialPublishError,
            "^youtube_readback_mismatch$",
        ):
            YouTubeVideoManagementClient(transport).read_status(
                "access-secret",
                video_id="video-1",
            )


if __name__ == "__main__":
    unittest.main()
