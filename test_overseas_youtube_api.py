# -*- coding: utf-8 -*-
"""Offline contract tests for the zero-write YouTube API boundary."""

import inspect
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import app_core.overseas_youtube_api as youtube_api
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


class FakeUploadResponse:
    def __init__(
        self,
        status_code: object,
        payload: object,
        *,
        headers: object | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = {} if headers is None else headers

    def json(self) -> object:
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


class FakeYouTubeUploadTransport:
    def __init__(
        self,
        *,
        post_responses: list[object] | None = None,
        put_responses: list[object] | None = None,
        get_responses: list[object] | None = None,
    ) -> None:
        self.post_responses = list(post_responses or [])
        self.put_responses = list(put_responses or [])
        self.get_responses = list(get_responses or [])
        self.post_calls: list[dict[str, object]] = []
        self.put_calls: list[dict[str, object]] = []
        self.get_calls: list[dict[str, object]] = []

    @staticmethod
    def _next(responses: list[object]) -> object:
        if not responses:
            raise AssertionError("unexpected transport call")
        response = responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def post(self, url: str, **kwargs: object) -> object:
        self.post_calls.append({"url": url, **kwargs})
        return self._next(self.post_responses)

    def put(self, url: str, **kwargs: object) -> object:
        self.put_calls.append({"url": url, **kwargs})
        return self._next(self.put_responses)

    def get(self, url: str, **kwargs: object) -> object:
        self.get_calls.append({"url": url, **kwargs})
        return self._next(self.get_responses)


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


class YouTubePrivateResumableUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.video = Path(self._temporary_directory.name) / "video.mp4"
        self.video.write_bytes(b"abcdefghij")
        self.validated = local_preflight(
            YouTubeUploadRequest(
                video_paths=(self.video,),
                title="Exact private title",
                description="Private description",
                tags=("one", "two"),
                category_id="22",
                made_for_kids=False,
                notify_subscribers=False,
            )
        )
        self.identity = YouTubeChannelIdentity(channel_id="UC_expected", display_name="Expected")
        self.token = "access-token-secret"
        self.session_uri = "https://upload.youtube.example/sensitive-session-token"

    def _adapter(self, transport: FakeYouTubeUploadTransport) -> Any:
        self.assertTrue(
            hasattr(youtube_api, "YouTubePrivateUploadAdapter"),
            "private resumable upload adapter is not implemented",
        )
        return youtube_api.YouTubePrivateUploadAdapter(transport)

    def _error_type(self) -> type[Exception]:
        self.assertTrue(
            hasattr(youtube_api, "YouTubeUploadError"),
            "stable upload error is not implemented",
        )
        return youtube_api.YouTubeUploadError

    def _receipt(self, state: str = "uploaded_private", video_id: str = "video_exact") -> Any:
        self.assertTrue(
            hasattr(youtube_api, "YouTubeUploadReceipt"),
            "private upload receipt is not implemented",
        )
        return youtube_api.YouTubeUploadReceipt(state=state, video_id=video_id)

    def _session_response(self) -> FakeUploadResponse:
        return FakeUploadResponse(200, {}, headers={"Location": self.session_uri})

    def _matching_item(
        self,
        *,
        video_id: str = "video_exact",
        channel_id: str = "UC_expected",
        title: str = "Exact private title",
        privacy_status: str = "private",
        upload_status: str = "processed",
        processing_status: str | None = "succeeded",
    ) -> dict[str, object]:
        item: dict[str, object] = {
            "id": video_id,
            "snippet": {"channelId": channel_id, "title": title},
            "status": {"privacyStatus": privacy_status, "uploadStatus": upload_status},
        }
        if processing_status is not None:
            item["processingDetails"] = {"processingStatus": processing_status}
        return item

    def _begin(self, adapter: Any) -> Any:
        return adapter.initiate_resumable_upload(
            self.validated,
            self.identity,
            self.token,
            confirmed_current=True,
        )

    def test_initiation_requires_current_confirmation_validated_request_identity_and_token(self) -> None:
        transport = FakeYouTubeUploadTransport()
        adapter = self._adapter(transport)
        error_type = self._error_type()

        for confirmation in (False, None, 1):
            with self.subTest(confirmation=confirmation):
                with self.assertRaisesRegex(error_type, "^authorization_denied$"):
                    adapter.initiate_resumable_upload(
                        self.validated,
                        self.identity,
                        self.token,
                        confirmed_current=confirmation,
                    )
        with self.assertRaisesRegex(error_type, "^authorization_invalid$"):
            adapter.initiate_resumable_upload(
                YouTubeUploadRequest(video_paths=(self.video,), title="raw", description="raw"),
                self.identity,
                self.token,
                confirmed_current=True,
            )
        with self.assertRaisesRegex(error_type, "^authorization_invalid$"):
            adapter.initiate_resumable_upload(
                self.validated,
                object(),
                self.token,
                confirmed_current=True,
            )
        with self.assertRaisesRegex(error_type, "^credential_unavailable$"):
            adapter.initiate_resumable_upload(
                self.validated,
                self.identity,
                "",
                confirmed_current=True,
            )

        self.assertEqual(transport.post_calls, [])

    def test_successful_private_upload_uses_one_session_and_verifies_only_the_exact_video_id(self) -> None:
        transport = FakeYouTubeUploadTransport(
            post_responses=[self._session_response()],
            put_responses=[FakeUploadResponse(201, {"id": "video_exact"})],
            get_responses=[FakeUploadResponse(200, {"items": [self._matching_item()]})],
        )
        adapter = self._adapter(transport)

        session = self._begin(adapter)

        self.assertEqual(len(transport.post_calls), 1)
        initiation = transport.post_calls[0]
        self.assertEqual(initiation["url"], youtube_api.YOUTUBE_UPLOADS_ENDPOINT)
        self.assertEqual(
            initiation["params"],
            {
                "uploadType": "resumable",
                "part": "snippet,status",
                "notifySubscribers": False,
            },
        )
        self.assertEqual(initiation["json"], self.validated.metadata())
        self.assertEqual(
            initiation["headers"],
            {
                "Authorization": "Bearer access-token-secret",
                "X-Upload-Content-Length": "10",
                "X-Upload-Content-Type": "application/octet-stream",
            },
        )
        self.assertGreater(initiation["timeout"], 0)
        self.assertNotIn(self.session_uri, repr(session))
        self.assertNotIn(self.token, repr(session))

        uploaded = adapter.upload_or_resume(session, self.token)

        self.assertEqual(uploaded, self._receipt())
        self.assertEqual(len(transport.put_calls), 1)
        media_call = transport.put_calls[0]
        self.assertEqual(media_call["url"], self.session_uri)
        self.assertEqual(media_call["data"], b"abcdefghij")
        self.assertEqual(
            media_call["headers"],
            {
                "Authorization": "Bearer access-token-secret",
                "Content-Length": "10",
                "Content-Type": "application/octet-stream",
                "Content-Range": "bytes 0-9/10",
            },
        )
        self.assertNotIn(self.session_uri, repr(uploaded))
        self.assertNotIn(self.token, repr(uploaded))

        verified = adapter.verify_exact_readback(
            uploaded,
            self.validated,
            self.identity,
            self.token,
        )

        self.assertEqual(verified, self._receipt(state="processed_private"))
        self.assertEqual(len(transport.get_calls), 1)
        self.assertEqual(
            transport.get_calls[0],
            {
                "url": youtube_api.YOUTUBE_VIDEOS_ENDPOINT,
                "headers": {"Authorization": "Bearer access-token-secret"},
                "params": {
                    "part": "id,snippet,status,processingDetails",
                    "id": "video_exact",
                },
                "timeout": transport.get_calls[0]["timeout"],
            },
        )
        self.assertGreater(transport.get_calls[0]["timeout"], 0)
        self.assertNotIn("public", verified.state)
        with self.assertRaises(FrozenInstanceError):
            verified.state = "public"  # type: ignore[misc]

    def test_308_continues_the_same_session_without_a_second_insert(self) -> None:
        self.video.write_bytes(b"abcdefgh")
        self.validated = local_preflight(
            YouTubeUploadRequest(
                video_paths=(self.video,),
                title="Exact private title",
                description="Private description",
                notify_subscribers=False,
            )
        )
        transport = FakeYouTubeUploadTransport(
            post_responses=[self._session_response()],
            put_responses=[
                FakeUploadResponse(308, {}, headers={"Range": "bytes=0-3"}),
                FakeUploadResponse(201, {"id": "video_exact"}),
            ],
        )
        adapter = self._adapter(transport)
        session = self._begin(adapter)

        incomplete = adapter.upload_or_resume(session, self.token)
        completed = adapter.upload_or_resume(session, self.token)

        self.assertEqual(incomplete, self._receipt(state="upload_started", video_id=None))
        self.assertEqual(completed, self._receipt())
        self.assertEqual(len(transport.post_calls), 1)
        self.assertEqual(len(transport.put_calls), 2)
        self.assertEqual([call["url"] for call in transport.put_calls], [self.session_uri, self.session_uri])
        self.assertEqual(transport.put_calls[0]["data"], b"abcdefgh")
        self.assertEqual(transport.put_calls[0]["headers"]["Content-Range"], "bytes 0-7/8")
        self.assertEqual(transport.put_calls[1]["data"], b"efgh")
        self.assertEqual(transport.put_calls[1]["headers"]["Content-Range"], "bytes 4-7/8")

    def test_upload_rejects_a_session_returned_by_another_adapter(self) -> None:
        first_transport = FakeYouTubeUploadTransport(post_responses=[self._session_response()])
        second_transport = FakeYouTubeUploadTransport(post_responses=[self._session_response()])
        first_adapter = self._adapter(first_transport)
        second_adapter = self._adapter(second_transport)
        self._begin(first_adapter)
        foreign_session = self._begin(second_adapter)

        with self.assertRaisesRegex(self._error_type(), "^upload_session_invalid$"):
            first_adapter.upload_or_resume(foreign_session, self.token)

        self.assertEqual(first_transport.put_calls, [])
        self.assertEqual(second_transport.put_calls, [])

    def test_missing_location_and_ambiguous_initiation_stop_without_another_insert(self) -> None:
        cases = (
            FakeUploadResponse(200, {}, headers={}),
            FakeUploadResponse("invalid-status", {}, headers={"Location": self.session_uri}),
            TimeoutError("provider-timeout-with-session-secret"),
        )
        for response in cases:
            with self.subTest(response=type(response).__name__):
                transport = FakeYouTubeUploadTransport(post_responses=[response])
                adapter = self._adapter(transport)
                with self.assertRaisesRegex(self._error_type(), "^outcome_unknown$") as raised:
                    self._begin(adapter)
                self.assertEqual(len(transport.post_calls), 1)
                self.assertNotIn(self.session_uri, str(raised.exception))
                self.assertNotIn(self.token, str(raised.exception))
                self.assertNotIn("provider-timeout", str(raised.exception))

    def test_missing_video_id_or_ambiguous_upload_returns_outcome_unknown_without_new_insert(self) -> None:
        cases = (
            FakeUploadResponse(201, {}),
            FakeUploadResponse(201, RuntimeError("raw-provider-body")),
            FakeUploadResponse("invalid-status", {"id": "secret-provider-id"}),
            FakeUploadResponse(503, {"detail": "raw-provider-body"}),
            TimeoutError("provider-timeout"),
        )
        for response in cases:
            with self.subTest(response=type(response).__name__):
                transport = FakeYouTubeUploadTransport(
                    post_responses=[self._session_response()],
                    put_responses=[response],
                )
                adapter = self._adapter(transport)
                session = self._begin(adapter)

                result = adapter.upload_or_resume(session, self.token)

                self.assertEqual(result, self._receipt(state="outcome_unknown", video_id=None))
                self.assertEqual(len(transport.post_calls), 1)
                self.assertEqual(len(transport.put_calls), 1)
                self.assertNotIn(self.session_uri, repr(result))
                self.assertNotIn(self.token, repr(result))
                self.assertNotIn("raw-provider-body", repr(result))

    def test_definite_upload_rejection_is_a_stable_failure_without_provider_leakage(self) -> None:
        transport = FakeYouTubeUploadTransport(
            post_responses=[self._session_response()],
            put_responses=[FakeUploadResponse(400, {"detail": "raw-provider-body"})],
        )
        adapter = self._adapter(transport)
        session = self._begin(adapter)

        with self.assertRaisesRegex(self._error_type(), "^upload_failed$") as raised:
            adapter.upload_or_resume(session, self.token)

        self.assertEqual(len(transport.post_calls), 1)
        self.assertEqual(len(transport.put_calls), 1)
        self.assertNotIn("raw-provider-body", str(raised.exception))
        self.assertNotIn(self.session_uri, str(raised.exception))
        self.assertNotIn(self.token, str(raised.exception))

    def test_readback_rejects_empty_duplicate_or_mismatched_exact_id_results(self) -> None:
        matching = self._matching_item()
        cases = (
            [],
            [matching, matching],
            [self._matching_item(video_id="video_other")],
            [self._matching_item(channel_id="UC_other")],
            [self._matching_item(title="Different title")],
            [self._matching_item(privacy_status="public")],
        )
        for items in cases:
            with self.subTest(items=items):
                transport = FakeYouTubeUploadTransport(
                    get_responses=[FakeUploadResponse(200, {"items": items, "detail": "raw-provider-body"})]
                )
                adapter = self._adapter(transport)

                with self.assertRaisesRegex(self._error_type(), "^readback_mismatch$") as raised:
                    adapter.verify_exact_readback(
                        self._receipt(),
                        self.validated,
                        self.identity,
                        self.token,
                    )

                self.assertEqual(len(transport.get_calls), 1)
                self.assertEqual(transport.get_calls[0]["params"]["id"], "video_exact")
                self.assertNotIn("raw-provider-body", str(raised.exception))
                self.assertNotIn(self.token, str(raised.exception))

    def test_readback_maps_processing_success_failure_and_rejection_without_publication(self) -> None:
        success_cases = (
            (self._matching_item(upload_status="uploaded", processing_status="processing"), "processing"),
            (self._matching_item(upload_status="processed", processing_status="succeeded"), "processed_private"),
            (self._matching_item(upload_status="processed", processing_status=None), "processed_private"),
        )
        for item, expected_state in success_cases:
            with self.subTest(expected_state=expected_state):
                transport = FakeYouTubeUploadTransport(
                    get_responses=[FakeUploadResponse(200, {"items": [item]})]
                )
                adapter = self._adapter(transport)

                result = adapter.verify_exact_readback(
                    self._receipt(),
                    self.validated,
                    self.identity,
                    self.token,
                )

                self.assertEqual(result, self._receipt(state=expected_state))
                self.assertNotIn("public", result.state)

        failure_cases = (
            (self._matching_item(upload_status="failed", processing_status="failed"), "processing_failed"),
            (self._matching_item(upload_status="failed", processing_status="terminated"), "processing_failed"),
            (self._matching_item(upload_status="rejected", processing_status=None), "rejected"),
        )
        for item, expected_reason in failure_cases:
            with self.subTest(expected_reason=expected_reason):
                transport = FakeYouTubeUploadTransport(
                    get_responses=[FakeUploadResponse(200, {"items": [item]})]
                )
                adapter = self._adapter(transport)
                with self.assertRaisesRegex(self._error_type(), f"^{expected_reason}$"):
                    adapter.verify_exact_readback(
                        self._receipt(),
                        self.validated,
                        self.identity,
                        self.token,
                    )

    def test_readback_transport_and_response_ambiguity_are_stable_outcome_unknown(self) -> None:
        cases = (
            TimeoutError("provider-timeout"),
            FakeUploadResponse("invalid-status", {"detail": "raw-provider-body"}),
            FakeUploadResponse(503, {"detail": "raw-provider-body"}),
            FakeUploadResponse(200, RuntimeError("raw-provider-body")),
        )
        for response in cases:
            with self.subTest(response=type(response).__name__):
                transport = FakeYouTubeUploadTransport(get_responses=[response])
                adapter = self._adapter(transport)

                with self.assertRaisesRegex(self._error_type(), "^outcome_unknown$") as raised:
                    adapter.verify_exact_readback(
                        self._receipt(),
                        self.validated,
                        self.identity,
                        self.token,
                    )

                self.assertEqual(len(transport.get_calls), 1)
                self.assertNotIn("raw-provider-body", str(raised.exception))
                self.assertNotIn("provider-timeout", str(raised.exception))
                self.assertNotIn(self.token, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
