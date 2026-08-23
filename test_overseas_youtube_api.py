# -*- coding: utf-8 -*-
"""Offline contract tests for the zero-write YouTube API boundary."""

import inspect
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

from app_core.overseas_youtube_api import (
    YOUTUBE_CHANNELS_ENDPOINT,
    YouTubeChannelIdentity,
    YouTubeChannelIdentityClient,
    YouTubeChannelLookupError,
    YouTubePreflightError,
    YouTubeUploadRequest,
    local_preflight,
)


class FakeChannelResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


class NonJsonChannelResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def json(self) -> object:
        raise RuntimeError("raw-provider-body")


class InvalidStatusChannelResponse:
    status_code = "not-an-http-status"

    def json(self) -> object:
        return {"items": [], "detail": "raw-provider-body"}


class FakeReadOnlyTransport:
    def __init__(self, response: FakeChannelResponse | None = None) -> None:
        self._response = response
        self.get_calls: list[tuple[str, dict[str, str], dict[str, str], float]] = []
        self.write_calls: list[object] = []

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, str],
        timeout: float,
    ) -> FakeChannelResponse:
        self.get_calls.append((url, headers, params, timeout))
        assert self._response is not None
        return self._response

    def post(self, *args: object, **kwargs: object) -> None:
        self.write_calls.append((args, kwargs))
        raise AssertionError("channel identity lookup must not write")


class YouTubeLocalPreflightTests(unittest.TestCase):
    def _request(self, video: Path, **changes: object) -> YouTubeUploadRequest:
        values: dict[str, object] = {
            "video_paths": (video,),
            "title": "A private test upload",
            "description": "Offline contract only.",
            "tags": ("offline", "contract"),
            "category_id": "22",
            "made_for_kids": False,
            "notify_subscribers": True,
        }
        values.update(changes)
        return YouTubeUploadRequest(**values)  # type: ignore[arg-type]

    def test_preflight_returns_one_immutable_private_request_and_serializes_supported_fields(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            video = Path(raw) / "video.mp4"
            video.write_bytes(b"video")

            checked = local_preflight(
                self._request(video, tags=("one", "two"), made_for_kids=True, notify_subscribers=False)
            )

        self.assertEqual(checked.video_path, video)
        self.assertEqual(checked.visibility, "private")
        self.assertEqual(
            checked.metadata(),
            {
                "snippet": {
                    "title": "A private test upload",
                    "description": "Offline contract only.",
                    "tags": ["one", "two"],
                    "categoryId": "22",
                },
                "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": True},
            },
        )
        self.assertEqual(checked.upload_parameters(), {"notifySubscribers": False})
        with self.assertRaises(FrozenInstanceError):
            checked.title = "changed"  # type: ignore[misc]

    def test_preflight_requires_exactly_one_readable_local_video(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first = root / "first.mp4"
            second = root / "second.mp4"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            cases = (
                ((), "video_count_invalid"),
                ((first, second), "video_count_invalid"),
                ((root / "missing.mp4",), "video_unreadable"),
                ((root,), "video_unreadable"),
            )
            for video_paths, reason in cases:
                with self.subTest(video_paths=video_paths):
                    with self.assertRaisesRegex(YouTubePreflightError, f"^{reason}$"):
                        local_preflight(self._request(first, video_paths=video_paths))

    def test_preflight_requires_a_nonempty_title_within_youtube_limit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            video = Path(raw) / "video.mp4"
            video.write_bytes(b"video")
            for title, reason in (("", "title_required"), ("   ", "title_required"), ("x" * 101, "title_too_long")):
                with self.subTest(title=title):
                    with self.assertRaisesRegex(YouTubePreflightError, f"^{reason}$"):
                        local_preflight(self._request(video, title=title))

    def test_preflight_requires_description_within_utf8_byte_limit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            video = Path(raw) / "video.mp4"
            video.write_bytes(b"video")
            self.assertEqual(
                local_preflight(self._request(video, description="中" * 1666 + "a")).description,
                "中" * 1666 + "a",
            )
            with self.assertRaisesRegex(YouTubePreflightError, "^description_too_long$"):
                local_preflight(self._request(video, description="中" * 1667))

    def test_preflight_rejects_private_incompatible_or_unsupported_fields(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            video = Path(raw) / "video.mp4"
            thumbnail = Path(raw) / "thumbnail.png"
            video.write_bytes(b"video")
            thumbnail.write_bytes(b"image")
            cases = (
                ({"visibility": "public"}, "visibility_private_required"),
                ({"visibility": "unlisted"}, "visibility_private_required"),
                ({"scheduled_at": "2026-08-24T12:00:00Z"}, "scheduling_unsupported"),
                ({"timer_enabled": True}, "scheduling_unsupported"),
                ({"playlist_id": "PL123"}, "playlist_unsupported"),
                ({"collection_id": "collection"}, "playlist_unsupported"),
                ({"thumbnail_path": thumbnail}, "thumbnail_unsupported"),
                ({"cover_path": thumbnail}, "thumbnail_unsupported"),
                ({"ai_declaration": False}, "ai_declaration_unsupported"),
            )
            for changes, reason in cases:
                with self.subTest(changes=changes):
                    with self.assertRaisesRegex(YouTubePreflightError, f"^{reason}$"):
                        local_preflight(self._request(video, **changes))

    def test_local_preflight_is_pure_and_never_uses_a_video_insert_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            video = Path(raw) / "video.mp4"
            video.write_bytes(b"video")
            local_preflight(self._request(video))

        source = inspect.getsource(local_preflight)
        self.assertNotIn("videos.insert", source)
        self.assertNotIn("http", source.lower())


class YouTubeChannelIdentityTests(unittest.TestCase):
    def _lookup(self, payload: object) -> tuple[YouTubeChannelIdentityClient, FakeReadOnlyTransport]:
        transport = FakeReadOnlyTransport(FakeChannelResponse(200, payload))
        return YouTubeChannelIdentityClient(transport), transport

    def test_lookup_returns_exactly_one_stable_channel_identity_without_tokens_or_provider_body(self) -> None:
        client, transport = self._lookup(
            {"items": [{"id": "UC_stable_channel", "snippet": {"title": "Channel Name"}}], "providerSecret": "raw-provider-body"}
        )

        identity = client.lookup_authenticated_channel("access-token-secret")

        self.assertEqual(identity, YouTubeChannelIdentity(channel_id="UC_stable_channel", display_name="Channel Name"))
        self.assertEqual(len(transport.get_calls), 1)
        url, headers, params, timeout = transport.get_calls[0]
        self.assertEqual(url, YOUTUBE_CHANNELS_ENDPOINT)
        self.assertEqual(headers, {"Authorization": "Bearer access-token-secret"})
        self.assertEqual(params, {"part": "id,snippet", "mine": "true"})
        self.assertGreater(timeout, 0)
        self.assertEqual(transport.write_calls, [])
        self.assertNotIn("access-token-secret", repr(identity))
        self.assertNotIn("raw-provider-body", repr(identity))
        with self.assertRaises(FrozenInstanceError):
            identity.channel_id = "changed"  # type: ignore[misc]

    def test_lookup_rejects_empty_or_ambiguous_channel_results_without_echoing_provider_body(self) -> None:
        cases = (
            ({"items": [], "detail": "raw-provider-body"}, "channel_not_found"),
            ({"items": [{"id": "UC_one"}, {"id": "UC_two"}], "detail": "raw-provider-body"}, "channel_identity_ambiguous"),
        )
        for payload, reason in cases:
            with self.subTest(reason=reason):
                client, _transport = self._lookup(payload)
                with self.assertRaisesRegex(YouTubeChannelLookupError, f"^{reason}$") as raised:
                    client.lookup_authenticated_channel("access-token-secret")
                self.assertNotIn("access-token-secret", str(raised.exception))
                self.assertNotIn("raw-provider-body", str(raised.exception))

    def test_non_success_status_is_rejected_before_parsing_any_body(self) -> None:
        cases = (
            FakeChannelResponse(401, ["raw-provider-body"]),
            NonJsonChannelResponse(500),
        )
        for response in cases:
            with self.subTest(status=response.status_code):
                transport = FakeReadOnlyTransport(response)  # type: ignore[arg-type]
                client = YouTubeChannelIdentityClient(transport)
                with self.assertRaisesRegex(YouTubeChannelLookupError, "^channel_lookup_rejected$") as raised:
                    client.lookup_authenticated_channel("access-token-secret")
                self.assertNotIn("access-token-secret", str(raised.exception))
                self.assertNotIn("raw-provider-body", str(raised.exception))

    def test_invalid_status_type_is_normalized_without_parsing_or_echoing_body(self) -> None:
        transport = FakeReadOnlyTransport(InvalidStatusChannelResponse())  # type: ignore[arg-type]
        client = YouTubeChannelIdentityClient(transport)

        with self.assertRaisesRegex(YouTubeChannelLookupError, "^channel_lookup_response_invalid$") as raised:
            client.lookup_authenticated_channel("access-token-secret")

        self.assertNotIn("access-token-secret", str(raised.exception))
        self.assertNotIn("raw-provider-body", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
