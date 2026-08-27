# -*- coding: utf-8 -*-
"""Offline contract tests for the zero-write YouTube API boundary."""

import tempfile
import threading
import unittest
from collections.abc import Iterator, Mapping
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any
from unittest.mock import patch

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
        self.get_calls: list[
            tuple[str, dict[str, str], dict[str, str], float, bool]
        ] = []
        self.write_calls: list[object] = []

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, str],
        timeout: float,
        allow_redirects: bool,
    ) -> FakeChannelResponse:
        self.get_calls.append((url, headers, params, timeout, allow_redirects))
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


class RaisingAccessMapping(Mapping[str, object]):
    def __init__(self, secret: str) -> None:
        self._secret = secret

    def __getitem__(self, key: str) -> object:
        raise RuntimeError(self._secret)

    def __iter__(self) -> Iterator[str]:
        raise RuntimeError(self._secret)

    def __len__(self) -> int:
        raise RuntimeError(self._secret)

    def get(self, key: str, default: object = None) -> object:
        raise RuntimeError(self._secret)

    def items(self) -> object:
        raise RuntimeError(self._secret)


class RaisingStripString(str):
    def strip(self, chars: str | None = None) -> str:
        raise RuntimeError(str(self))


class FakeYouTubeUploadTransport:
    def __init__(
        self,
        *,
        post_responses: list[object] | None = None,
        put_responses: list[object] | None = None,
        get_responses: list[object] | None = None,
        channel_responses: list[object] | None = None,
    ) -> None:
        self.post_responses = list(post_responses or [])
        self.put_responses = list(put_responses or [])
        self.get_responses = list(get_responses or [])
        self.channel_responses = (
            None if channel_responses is None else list(channel_responses)
        )
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
        if url == YOUTUBE_CHANNELS_ENDPOINT:
            if self.channel_responses is None:
                return FakeUploadResponse(
                    200,
                    {
                        "items": [
                            {
                                "id": "UC_expected",
                                "snippet": {"title": "Expected"},
                            }
                        ]
                    },
                )
            return self._next(self.channel_responses)
        return self._next(self.get_responses)


class BlockingUploadTransport(FakeYouTubeUploadTransport):
    """Deterministically exposes overlapping initiation or transfer calls."""

    def __init__(
        self,
        *,
        block_operation: str,
        post_responses: list[object] | None = None,
        put_responses: list[object] | None = None,
    ) -> None:
        super().__init__(
            post_responses=post_responses,
            put_responses=put_responses,
        )
        self.block_operation = block_operation
        self.first_entered = threading.Event()
        self.second_entered = threading.Event()
        self.release_first = threading.Event()
        self._call_lock = threading.Lock()
        self._active_calls = 0
        self.maximum_active_calls = 0

    def _enter(self, operation: str) -> None:
        if operation != self.block_operation:
            return
        with self._call_lock:
            self._active_calls += 1
            self.maximum_active_calls = max(
                self.maximum_active_calls,
                self._active_calls,
            )
            call_number = self._active_calls
        if call_number == 1:
            self.first_entered.set()
            self.release_first.wait(timeout=2.0)
        else:
            self.second_entered.set()

    def _leave(self, operation: str) -> None:
        if operation != self.block_operation:
            return
        with self._call_lock:
            self._active_calls -= 1

    def post(self, url: str, **kwargs: object) -> object:
        self._enter("post")
        try:
            return super().post(url, **kwargs)
        finally:
            self._leave("post")

    def put(self, url: str, **kwargs: object) -> object:
        self._enter("put")
        try:
            return super().put(url, **kwargs)
        finally:
            self._leave("put")


class ChunkRecordingTransport(FakeYouTubeUploadTransport):
    """Record chunk boundaries without retaining media bytes in the test."""

    def put(self, url: str, **kwargs: object) -> object:
        media = kwargs.pop("data")
        assert isinstance(media, bytes)
        self.put_calls.append(
            {
                "url": url,
                **kwargs,
                "data_length": len(media),
            }
        )
        return self._next(self.put_responses)


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

    def test_local_preflight_never_constructs_a_remote_upload_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            video = Path(raw) / "video.mp4"
            video.write_bytes(b"video")
            with patch.object(youtube_api, "YouTubePrivateUploadAdapter") as adapter:
                checked = local_preflight(self._request(video))

        adapter.assert_not_called()
        self.assertEqual(checked.video_path, video)


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
        url, headers, params, timeout, allow_redirects = transport.get_calls[0]
        self.assertEqual(url, YOUTUBE_CHANNELS_ENDPOINT)
        self.assertEqual(headers, {"Authorization": "Bearer access-token-secret"})
        self.assertEqual(params, {"part": "id,snippet", "mine": "true"})
        self.assertGreater(timeout, 0)
        self.assertIs(allow_redirects, False)
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
        self.session_uri = (
            "https://www.googleapis.com/upload/youtube/v3/videos"
            "?upload_id=sensitive-session-token"
        )

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

    def _channel_response(self, channel_id: str = "UC_expected") -> FakeUploadResponse:
        return FakeUploadResponse(
            200,
            {"items": [{"id": channel_id, "snippet": {"title": "Expected"}}]},
        )

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
        plan = adapter.create_confirmed_upload_plan(
            self.validated,
            credential_reference="credential-reference-secret",
            expected_channel=self.identity,
            access_token=self.token,
            confirmed_current=True,
        )
        return adapter.initiate_resumable_upload(plan)

    def _complete_upload(self, adapter: Any) -> Any:
        return adapter.upload_or_resume(self._begin(adapter))

    def test_plan_requires_validated_request_and_stable_channel_identity(self) -> None:
        transport = FakeYouTubeUploadTransport()
        adapter = self._adapter(transport)
        error_type = self._error_type()

        with self.assertRaisesRegex(error_type, "^authorization_invalid$"):
            adapter.create_confirmed_upload_plan(
                YouTubeUploadRequest(video_paths=(self.video,), title="raw", description="raw"),
                credential_reference="credential-reference-secret",
                expected_channel=self.identity,
                access_token=self.token,
                confirmed_current=True,
            )
        with self.assertRaisesRegex(error_type, "^authorization_invalid$"):
            adapter.create_confirmed_upload_plan(
                self.validated,
                credential_reference="credential-reference-secret",
                expected_channel=object(),
                access_token=self.token,
                confirmed_current=True,
            )
        forged_validated = youtube_api.ValidatedYouTubeUpload(
            video_path=self.video,
            title="",
            description="forged without local preflight",
            tags=(),
            category_id=None,
            made_for_kids=False,
            notify_subscribers=False,
        )
        with self.assertRaisesRegex(error_type, "^authorization_invalid$"):
            adapter.create_confirmed_upload_plan(
                forged_validated,
                credential_reference="credential-reference-secret",
                expected_channel=self.identity,
                access_token=self.token,
                confirmed_current=True,
            )

        self.assertEqual(transport.post_calls, [])
        self.assertEqual(transport.get_calls, [])

    def test_adapter_owned_plan_is_opaque_and_revalidates_bound_channel_before_insert(self) -> None:
        transport = FakeYouTubeUploadTransport(
            post_responses=[self._session_response()],
            channel_responses=[self._channel_response()],
        )
        adapter = self._adapter(transport)
        self.assertTrue(
            hasattr(adapter, "create_confirmed_upload_plan"),
            "adapter-owned confirmed upload plan is not implemented",
        )

        plan = adapter.create_confirmed_upload_plan(
            self.validated,
            credential_reference="credential-reference-secret",
            expected_channel=self.identity,
            access_token=self.token,
            confirmed_current=True,
        )

        public_plan = f"{plan!r} {plan!s}"
        for secret in (
            str(self.video),
            "credential-reference-secret",
            self.identity.channel_id,
            self.identity.display_name,
            self.token,
            self.validated.title,
        ):
            self.assertNotIn(secret, public_plan)
        for authority_field in (
            "video_path",
            "credential_reference",
            "expected_channel",
            "access_token",
            "confirmed_current",
        ):
            self.assertFalse(hasattr(plan, authority_field))
        with self.assertRaises((AttributeError, FrozenInstanceError, TypeError)):
            plan.access_token = "replacement"  # type: ignore[attr-defined]

        session = adapter.initiate_resumable_upload(plan)

        self.assertIsNotNone(session)
        self.assertEqual(len(transport.get_calls), 1)
        self.assertEqual(transport.get_calls[0]["url"], YOUTUBE_CHANNELS_ENDPOINT)
        self.assertEqual(
            transport.get_calls[0]["headers"],
            {"Authorization": "Bearer access-token-secret"},
        )
        self.assertEqual(len(transport.post_calls), 1)
        with self.assertRaisesRegex(self._error_type(), "^upload_plan_used$"):
            adapter.initiate_resumable_upload(plan)
        self.assertEqual(len(transport.post_calls), 1)

    def test_plan_rejects_confirmation_credentials_channel_file_and_foreign_or_reused_handle_before_insert(self) -> None:
        error_type = self._error_type()
        adapter = self._adapter(FakeYouTubeUploadTransport())
        self.assertTrue(
            hasattr(adapter, "create_confirmed_upload_plan"),
            "adapter-owned confirmed upload plan is not implemented",
        )
        for confirmation in (False, None, 1):
            with self.subTest(confirmation=confirmation):
                with self.assertRaisesRegex(error_type, "^authorization_denied$"):
                    adapter.create_confirmed_upload_plan(
                        self.validated,
                        credential_reference="credential-reference-secret",
                        expected_channel=self.identity,
                        access_token=self.token,
                        confirmed_current=confirmation,
                    )
        for credential_reference, access_token, reason in (
            ("", self.token, "credential_unavailable"),
            ("   ", self.token, "credential_unavailable"),
            ("credential-reference-secret", "", "credential_unavailable"),
        ):
            with self.subTest(reason=reason, credential_reference=credential_reference):
                with self.assertRaisesRegex(error_type, f"^{reason}$"):
                    adapter.create_confirmed_upload_plan(
                        self.validated,
                        credential_reference=credential_reference,
                        expected_channel=self.identity,
                        access_token=access_token,
                        confirmed_current=True,
                    )

        changed_file_transport = FakeYouTubeUploadTransport(
            channel_responses=[self._channel_response()],
        )
        changed_file_adapter = self._adapter(changed_file_transport)
        changed_file_plan = changed_file_adapter.create_confirmed_upload_plan(
            self.validated,
            credential_reference="credential-reference-secret",
            expected_channel=self.identity,
            access_token=self.token,
            confirmed_current=True,
        )
        self.video.write_bytes(b"substituted-media")
        with self.assertRaisesRegex(error_type, "^upload_file_changed$"):
            changed_file_adapter.initiate_resumable_upload(changed_file_plan)
        self.assertEqual(changed_file_transport.post_calls, [])

        self.video.write_bytes(b"abcdefghij")
        wrong_channel_transport = FakeYouTubeUploadTransport(
            channel_responses=[self._channel_response("UC_substituted")],
        )
        wrong_channel_adapter = self._adapter(wrong_channel_transport)
        wrong_channel_plan = wrong_channel_adapter.create_confirmed_upload_plan(
            self.validated,
            credential_reference="credential-reference-secret",
            expected_channel=self.identity,
            access_token=self.token,
            confirmed_current=True,
        )
        with self.assertRaisesRegex(error_type, "^authorization_invalid$"):
            wrong_channel_adapter.initiate_resumable_upload(wrong_channel_plan)
        self.assertEqual(wrong_channel_transport.post_calls, [])

        failure_transport = FakeYouTubeUploadTransport(
            channel_responses=[self._channel_response()],
            post_responses=[FakeUploadResponse(400, {"detail": "provider-secret"})],
        )
        failure_adapter = self._adapter(failure_transport)
        one_use_plan = failure_adapter.create_confirmed_upload_plan(
            self.validated,
            credential_reference="credential-reference-secret",
            expected_channel=self.identity,
            access_token=self.token,
            confirmed_current=True,
        )
        with self.assertRaisesRegex(error_type, "^upload_failed$"):
            failure_adapter.initiate_resumable_upload(one_use_plan)
        with self.assertRaisesRegex(error_type, "^upload_plan_used$"):
            failure_adapter.initiate_resumable_upload(one_use_plan)
        self.assertEqual(len(failure_transport.post_calls), 1)

        foreign_adapter = self._adapter(FakeYouTubeUploadTransport())
        with self.assertRaisesRegex(error_type, "^upload_plan_invalid$"):
            foreign_adapter.initiate_resumable_upload(one_use_plan)

    def test_concurrent_initiation_atomically_claims_exactly_one_insert(self) -> None:
        transport = BlockingUploadTransport(
            block_operation="post",
            post_responses=[self._session_response(), self._session_response()],
        )
        adapter = self._adapter(transport)
        plan = adapter.create_confirmed_upload_plan(
            self.validated,
            credential_reference="credential-reference-secret",
            expected_channel=self.identity,
            access_token=self.token,
            confirmed_current=True,
        )
        start_second = threading.Event()
        successes: list[object] = []
        errors: list[Exception] = []

        def initiate(*, wait_for_second: bool) -> None:
            if wait_for_second:
                start_second.wait(timeout=2.0)
            try:
                successes.append(adapter.initiate_resumable_upload(plan))
            except Exception as error:
                errors.append(error)

        first = threading.Thread(target=initiate, kwargs={"wait_for_second": False})
        second = threading.Thread(target=initiate, kwargs={"wait_for_second": True})
        first.start()
        self.assertTrue(transport.first_entered.wait(timeout=1.0))
        second.start()
        start_second.set()
        overlapping_insert = transport.second_entered.wait(timeout=0.5)
        transport.release_first.set()
        first.join(timeout=2.0)
        second.join(timeout=2.0)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertFalse(overlapping_insert)
        self.assertEqual(len(transport.post_calls), 1)
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(errors), 1)
        self.assertRegex(str(errors[0]), "^upload_plan_used$")

    def test_concurrent_transfer_serializes_put_and_closes_competing_worker(self) -> None:
        transport = BlockingUploadTransport(
            block_operation="put",
            post_responses=[self._session_response()],
            put_responses=[
                FakeUploadResponse(201, {"id": "video_exact"}),
                FakeUploadResponse(201, {"id": "video_exact"}),
            ],
        )
        adapter = self._adapter(transport)
        session = self._begin(adapter)
        start_second = threading.Event()
        receipts: list[object] = []
        errors: list[Exception] = []

        def transfer(*, wait_for_second: bool) -> None:
            if wait_for_second:
                start_second.wait(timeout=2.0)
            try:
                receipts.append(adapter.upload_or_resume(session))
            except Exception as error:
                errors.append(error)

        first = threading.Thread(target=transfer, kwargs={"wait_for_second": False})
        second = threading.Thread(target=transfer, kwargs={"wait_for_second": True})
        first.start()
        self.assertTrue(transport.first_entered.wait(timeout=1.0))
        second.start()
        start_second.set()
        overlapping_put = transport.second_entered.wait(timeout=0.5)
        transport.release_first.set()
        first.join(timeout=2.0)
        second.join(timeout=2.0)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertFalse(overlapping_put)
        self.assertEqual(transport.maximum_active_calls, 1)
        self.assertEqual(len(transport.put_calls), 1)
        self.assertEqual(receipts, [self._receipt()])
        self.assertEqual(len(errors), 1)
        self.assertRegex(str(errors[0]), "^upload_session_invalid$")

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

        uploaded = adapter.upload_or_resume(session)

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

        verified = adapter.verify_exact_readback(uploaded)

        self.assertEqual(verified, self._receipt(state="processed_private"))
        self.assertEqual(len(transport.get_calls), 2)
        video_readback = transport.get_calls[-1]
        self.assertEqual(
            video_readback,
            {
                "url": youtube_api.YOUTUBE_VIDEOS_ENDPOINT,
                "headers": {"Authorization": "Bearer access-token-secret"},
                "params": {
                    "part": "id,snippet,status,processingDetails",
                    "id": "video_exact",
                },
                "timeout": video_readback["timeout"],
                "allow_redirects": False,
            },
        )
        self.assertGreater(video_readback["timeout"], 0)
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

        incomplete = adapter.upload_or_resume(session)
        completed = adapter.upload_or_resume(session)

        self.assertEqual(incomplete, self._receipt(state="upload_started", video_id=None))
        self.assertEqual(completed, self._receipt())
        self.assertEqual(len(transport.post_calls), 1)
        self.assertEqual(len(transport.put_calls), 2)
        self.assertEqual([call["url"] for call in transport.put_calls], [self.session_uri, self.session_uri])
        self.assertEqual(transport.put_calls[0]["data"], b"abcdefgh")
        self.assertEqual(transport.put_calls[0]["headers"]["Content-Range"], "bytes 0-7/8")
        self.assertEqual(transport.put_calls[1]["data"], b"efgh")
        self.assertEqual(transport.put_calls[1]["headers"]["Content-Range"], "bytes 4-7/8")

    def test_sparse_media_is_read_and_sent_as_bounded_resumable_chunks(self) -> None:
        self.assertTrue(
            hasattr(youtube_api, "RESUMABLE_CHUNK_SIZE_BYTES"),
            "bounded resumable chunk size is not implemented",
        )
        chunk_size = youtube_api.RESUMABLE_CHUNK_SIZE_BYTES
        self.assertEqual(chunk_size, 8 * 1024 * 1024)
        self.assertEqual(chunk_size % (256 * 1024), 0)
        total_size = chunk_size + 17
        with self.video.open("wb") as sparse_video:
            sparse_video.truncate(total_size)
        self.validated = local_preflight(
            YouTubeUploadRequest(
                video_paths=(self.video,),
                title="Exact private title",
                description="Private description",
                notify_subscribers=False,
            )
        )
        transport = ChunkRecordingTransport(
            post_responses=[self._session_response()],
            put_responses=[
                FakeUploadResponse(
                    308,
                    {},
                    headers={"Range": f"bytes=0-{chunk_size - 1}"},
                ),
                FakeUploadResponse(201, {"id": "video_exact"}),
            ],
        )
        adapter = self._adapter(transport)
        session = self._begin(adapter)

        first = adapter.upload_or_resume(session)
        second = adapter.upload_or_resume(session)

        self.assertEqual(first, self._receipt(state="upload_started", video_id=None))
        self.assertEqual(second, self._receipt())
        self.assertEqual(len(transport.post_calls), 1)
        self.assertEqual(
            [call["data_length"] for call in transport.put_calls],
            [chunk_size, 17],
        )
        self.assertEqual(
            [call["headers"]["Content-Range"] for call in transport.put_calls],
            [
                f"bytes 0-{chunk_size - 1}/{total_size}",
                f"bytes {chunk_size}-{total_size - 1}/{total_size}",
            ],
        )

    def test_308_acknowledgement_cannot_advance_past_the_sent_chunk(self) -> None:
        chunk_size = 8 * 1024 * 1024
        total_size = chunk_size + 100
        with self.video.open("wb") as sparse_video:
            sparse_video.truncate(total_size)
        self.validated = local_preflight(
            YouTubeUploadRequest(
                video_paths=(self.video,),
                title="Exact private title",
                description="Private description",
                notify_subscribers=False,
            )
        )
        transport = ChunkRecordingTransport(
            post_responses=[self._session_response()],
            put_responses=[
                FakeUploadResponse(
                    308,
                    {},
                    headers={"Range": f"bytes=0-{chunk_size + 50}"},
                )
            ],
        )
        adapter = self._adapter(transport)
        session = self._begin(adapter)

        result = adapter.upload_or_resume(session)

        self.assertEqual(
            result,
            self._receipt(state="outcome_unknown", video_id=None),
        )
        self.assertEqual(transport.put_calls[0]["data_length"], chunk_size)
        with self.assertRaisesRegex(self._error_type(), "^outcome_unknown$"):
            adapter.upload_or_resume(session)
        self.assertEqual(len(transport.put_calls), 1)

    def test_upload_rejects_a_session_returned_by_another_adapter(self) -> None:
        first_transport = FakeYouTubeUploadTransport(post_responses=[self._session_response()])
        second_transport = FakeYouTubeUploadTransport(post_responses=[self._session_response()])
        first_adapter = self._adapter(first_transport)
        second_adapter = self._adapter(second_transport)
        self._begin(first_adapter)
        foreign_session = self._begin(second_adapter)

        with self.assertRaisesRegex(self._error_type(), "^upload_session_invalid$"):
            first_adapter.upload_or_resume(foreign_session)

        self.assertEqual(first_transport.put_calls, [])
        self.assertEqual(second_transport.put_calls, [])

    def test_session_handle_is_opaque_immutable_and_cannot_redirect_media(self) -> None:
        transport = FakeYouTubeUploadTransport(
            post_responses=[self._session_response()],
            put_responses=[FakeUploadResponse(201, {"id": "video_exact"})],
        )
        adapter = self._adapter(transport)
        session = self._begin(adapter)

        for routing_field in ("_session_uri", "_next_byte", "_total_size", "_upload", "_closed"):
            with self.subTest(routing_field=routing_field):
                self.assertFalse(hasattr(session, routing_field))
        self.assertTrue(hasattr(session, "_correlation"))
        with self.assertRaises((AttributeError, FrozenInstanceError, TypeError)):
            session._session_uri = "https://attacker.example/redirect"  # type: ignore[attr-defined]

        result = adapter.upload_or_resume(session)

        self.assertEqual(result, self._receipt())
        self.assertEqual(len(transport.put_calls), 1)
        self.assertEqual(transport.put_calls[0]["url"], self.session_uri)

        tamper_transport = FakeYouTubeUploadTransport(post_responses=[self._session_response()])
        tamper_adapter = self._adapter(tamper_transport)
        tampered_session = self._begin(tamper_adapter)
        object.__setattr__(tampered_session, "_correlation", object())
        with self.assertRaisesRegex(self._error_type(), "^upload_session_invalid$"):
            tamper_adapter.upload_or_resume(tampered_session)

        forged_session = object.__new__(youtube_api.YouTubeResumableSession)
        with self.assertRaisesRegex(self._error_type(), "^upload_session_invalid$"):
            tamper_adapter.upload_or_resume(forged_session)
        self.assertEqual(tamper_transport.put_calls, [])

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

    def test_resumable_location_rejects_unapproved_origin_authority_and_path_before_media(self) -> None:
        unsafe_locations = (
            "http://www.googleapis.com/upload/youtube/v3/videos?upload_id=secret",
            "https://attacker.example/upload/youtube/v3/videos?upload_id=secret",
            "https://www.googleapis.com.attacker.example/upload/youtube/v3/videos",
            "https://user:password@www.googleapis.com/upload/youtube/v3/videos",
            "https://www.googleapis.com:444/upload/youtube/v3/videos",
            "https://www.googleapis.com/upload/youtube/v3/videos#secret-fragment",
            "https://www.googleapis.com/youtube/v3/videos",
            "https://www.googleapis.com/upload/youtube/v3/videos/extra",
        )
        for location in unsafe_locations:
            with self.subTest(location=location):
                transport = FakeYouTubeUploadTransport(
                    post_responses=[FakeUploadResponse(200, {}, headers={"Location": location})]
                )
                adapter = self._adapter(transport)

                with self.assertRaisesRegex(self._error_type(), "^outcome_unknown$"):
                    self._begin(adapter)

                self.assertEqual(len(transport.post_calls), 1)
                self.assertEqual(transport.put_calls, [])
                self.assertNotEqual(transport.post_calls[0]["url"], location)

    def test_bearer_requests_disable_redirects_and_redirect_responses_are_terminal(self) -> None:
        redirect_target = "https://attacker.example/collect"

        channel_redirect = FakeYouTubeUploadTransport(
            channel_responses=[
                FakeUploadResponse(302, {}, headers={"Location": redirect_target})
            ]
        )
        channel_adapter = self._adapter(channel_redirect)
        channel_plan = channel_adapter.create_confirmed_upload_plan(
            self.validated,
            credential_reference="credential-reference-secret",
            expected_channel=self.identity,
            access_token=self.token,
            confirmed_current=True,
        )
        with self.assertRaisesRegex(self._error_type(), "^authorization_invalid$"):
            channel_adapter.initiate_resumable_upload(channel_plan)
        self.assertEqual(channel_redirect.post_calls, [])
        self.assertIn("allow_redirects", channel_redirect.get_calls[0])
        self.assertIs(channel_redirect.get_calls[0]["allow_redirects"], False)

        post_redirect = FakeYouTubeUploadTransport(
            post_responses=[
                FakeUploadResponse(302, {}, headers={"Location": redirect_target})
            ]
        )
        with self.assertRaisesRegex(self._error_type(), "^outcome_unknown$"):
            self._begin(self._adapter(post_redirect))
        self.assertIn("allow_redirects", post_redirect.post_calls[0])
        self.assertIs(post_redirect.post_calls[0]["allow_redirects"], False)
        self.assertEqual(post_redirect.put_calls, [])

        put_redirect = FakeYouTubeUploadTransport(
            post_responses=[self._session_response()],
            put_responses=[
                FakeUploadResponse(302, {}, headers={"Location": redirect_target})
            ],
        )
        put_adapter = self._adapter(put_redirect)
        put_result = put_adapter.upload_or_resume(self._begin(put_adapter))
        self.assertEqual(
            put_result,
            self._receipt(state="outcome_unknown", video_id=None),
        )
        self.assertEqual(len(put_redirect.put_calls), 1)
        self.assertEqual(put_redirect.put_calls[0]["url"], self.session_uri)
        self.assertNotEqual(put_redirect.put_calls[0]["url"], redirect_target)
        self.assertIn("allow_redirects", put_redirect.put_calls[0])
        self.assertIs(put_redirect.put_calls[0]["allow_redirects"], False)

        get_redirect = FakeYouTubeUploadTransport(
            post_responses=[self._session_response()],
            put_responses=[FakeUploadResponse(201, {"id": "video_exact"})],
            get_responses=[
                FakeUploadResponse(302, {}, headers={"Location": redirect_target})
            ],
        )
        get_adapter = self._adapter(get_redirect)
        uploaded = self._complete_upload(get_adapter)
        with self.assertRaisesRegex(self._error_type(), "^outcome_unknown$"):
            get_adapter.verify_exact_readback(uploaded)
        self.assertEqual(get_redirect.get_calls[-1]["url"], youtube_api.YOUTUBE_VIDEOS_ENDPOINT)
        self.assertNotEqual(get_redirect.get_calls[-1]["url"], redirect_target)
        self.assertIn("allow_redirects", get_redirect.get_calls[-1])
        self.assertIs(get_redirect.get_calls[-1]["allow_redirects"], False)

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

                result = adapter.upload_or_resume(session)

                self.assertEqual(result, self._receipt(state="outcome_unknown", video_id=None))
                with self.assertRaisesRegex(self._error_type(), "^outcome_unknown$"):
                    adapter.upload_or_resume(session)
                self.assertEqual(len(transport.post_calls), 1)
                self.assertEqual(len(transport.put_calls), 1)
                self.assertNotIn(self.session_uri, repr(result))
                self.assertNotIn(self.token, repr(result))
                self.assertNotIn("raw-provider-body", repr(result))

    def test_oversized_resumable_range_is_terminal_outcome_unknown(self) -> None:
        oversized_range = "bytes=0-" + ("9" * 5000)
        transport = FakeYouTubeUploadTransport(
            post_responses=[self._session_response()],
            put_responses=[
                FakeUploadResponse(
                    308,
                    {},
                    headers={"Range": oversized_range},
                )
            ],
        )
        adapter = self._adapter(transport)
        session = self._begin(adapter)

        result = adapter.upload_or_resume(session)

        self.assertEqual(result, self._receipt(state="outcome_unknown", video_id=None))
        with self.assertRaisesRegex(self._error_type(), "^outcome_unknown$") as raised:
            adapter.upload_or_resume(session)
        self.assertEqual(len(transport.post_calls), 1)
        self.assertEqual(len(transport.put_calls), 1)
        self.assertNotIn(self.session_uri, repr(result))
        self.assertNotIn(self.token, repr(result))
        self.assertNotIn(self.session_uri, str(raised.exception))
        self.assertNotIn(self.token, str(raised.exception))

    def test_definite_upload_rejection_is_a_stable_failure_without_provider_leakage(self) -> None:
        transport = FakeYouTubeUploadTransport(
            post_responses=[self._session_response()],
            put_responses=[FakeUploadResponse(400, {"detail": "raw-provider-body"})],
        )
        adapter = self._adapter(transport)
        session = self._begin(adapter)

        with self.assertRaisesRegex(self._error_type(), "^upload_failed$") as raised:
            adapter.upload_or_resume(session)

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
                    post_responses=[self._session_response()],
                    put_responses=[FakeUploadResponse(201, {"id": "video_exact"})],
                    get_responses=[FakeUploadResponse(200, {"items": items, "detail": "raw-provider-body"})]
                )
                adapter = self._adapter(transport)
                uploaded = self._complete_upload(adapter)

                with self.assertRaisesRegex(self._error_type(), "^readback_mismatch$") as raised:
                    adapter.verify_exact_readback(uploaded)

                self.assertEqual(len(transport.get_calls), 2)
                self.assertEqual(transport.get_calls[-1]["params"]["id"], "video_exact")
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
                    post_responses=[self._session_response()],
                    put_responses=[FakeUploadResponse(201, {"id": "video_exact"})],
                    get_responses=[FakeUploadResponse(200, {"items": [item]})]
                )
                adapter = self._adapter(transport)
                uploaded = self._complete_upload(adapter)

                result = adapter.verify_exact_readback(uploaded)

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
                    post_responses=[self._session_response()],
                    put_responses=[FakeUploadResponse(201, {"id": "video_exact"})],
                    get_responses=[FakeUploadResponse(200, {"items": [item]})]
                )
                adapter = self._adapter(transport)
                uploaded = self._complete_upload(adapter)
                with self.assertRaisesRegex(self._error_type(), f"^{expected_reason}$"):
                    adapter.verify_exact_readback(uploaded)

    def test_readback_transport_and_response_ambiguity_are_stable_outcome_unknown(self) -> None:
        cases = (
            TimeoutError("provider-timeout"),
            FakeUploadResponse("invalid-status", {"detail": "raw-provider-body"}),
            FakeUploadResponse(503, {"detail": "raw-provider-body"}),
            FakeUploadResponse(200, RuntimeError("raw-provider-body")),
            FakeUploadResponse(
                200,
                {
                    "items": [
                        self._matching_item(
                            upload_status="unknown-provider-state",
                            processing_status="unknown-provider-state",
                        )
                    ]
                },
            ),
        )
        for response in cases:
            with self.subTest(response=type(response).__name__):
                transport = FakeYouTubeUploadTransport(
                    post_responses=[self._session_response()],
                    put_responses=[FakeUploadResponse(201, {"id": "video_exact"})],
                    get_responses=[response],
                )
                adapter = self._adapter(transport)
                uploaded = self._complete_upload(adapter)

                with self.assertRaisesRegex(self._error_type(), "^outcome_unknown$") as raised:
                    adapter.verify_exact_readback(uploaded)
                with self.assertRaisesRegex(self._error_type(), "^outcome_unknown$"):
                    adapter.verify_exact_readback(uploaded)

                self.assertEqual(len(transport.get_calls), 2)
                self.assertNotIn("raw-provider-body", str(raised.exception))
                self.assertNotIn("provider-timeout", str(raised.exception))
                self.assertNotIn(self.token, str(raised.exception))

    def test_raising_header_mapping_is_normalized_without_leaking_secrets(self) -> None:
        provider_secret = f"raw-provider-body {self.session_uri} {self.token}"
        initiation_transport = FakeYouTubeUploadTransport(
            post_responses=[
                FakeUploadResponse(
                    200,
                    {},
                    headers=RaisingAccessMapping(provider_secret),
                )
            ]
        )
        initiation_adapter = self._adapter(initiation_transport)

        with self.assertRaisesRegex(self._error_type(), "^outcome_unknown$") as raised:
            self._begin(initiation_adapter)

        self.assertNotIn(provider_secret, str(raised.exception))
        self.assertEqual(len(initiation_transport.post_calls), 1)

        resume_transport = FakeYouTubeUploadTransport(
            post_responses=[self._session_response()],
            put_responses=[
                FakeUploadResponse(
                    308,
                    {},
                    headers=RaisingAccessMapping(provider_secret),
                )
            ],
        )
        resume_adapter = self._adapter(resume_transport)
        session = self._begin(resume_adapter)

        result = resume_adapter.upload_or_resume(session)

        self.assertEqual(result, self._receipt(state="outcome_unknown", video_id=None))
        with self.assertRaisesRegex(self._error_type(), "^outcome_unknown$"):
            resume_adapter.upload_or_resume(session)
        self.assertEqual(len(resume_transport.put_calls), 1)
        self.assertNotIn(provider_secret, repr(result))

    def test_raising_decoded_mapping_access_is_normalized_without_leaking_secrets(self) -> None:
        provider_secret = f"raw-provider-body {self.session_uri} {self.token}"
        upload_transport = FakeYouTubeUploadTransport(
            post_responses=[self._session_response()],
            put_responses=[FakeUploadResponse(201, RaisingAccessMapping(provider_secret))],
        )
        upload_adapter = self._adapter(upload_transport)
        upload_session = self._begin(upload_adapter)

        upload_result = upload_adapter.upload_or_resume(upload_session)

        self.assertEqual(upload_result, self._receipt(state="outcome_unknown", video_id=None))
        self.assertNotIn(provider_secret, repr(upload_result))

        readback_payloads = (
            RaisingAccessMapping(provider_secret),
            {"items": [RaisingAccessMapping(provider_secret)]},
            {
                "items": [
                    {
                        "id": "video_exact",
                        "snippet": RaisingAccessMapping(provider_secret),
                        "status": {"privacyStatus": "private", "uploadStatus": "processed"},
                    }
                ]
            },
            {
                "items": [
                    {
                        "id": "video_exact",
                        "snippet": {"channelId": "UC_expected", "title": "Exact private title"},
                        "status": RaisingAccessMapping(provider_secret),
                    }
                ]
            },
        )
        for payload in readback_payloads:
            with self.subTest(payload_type=type(payload).__name__):
                transport = FakeYouTubeUploadTransport(
                    post_responses=[self._session_response()],
                    put_responses=[FakeUploadResponse(201, {"id": "video_exact"})],
                    get_responses=[FakeUploadResponse(200, payload)],
                )
                adapter = self._adapter(transport)
                uploaded = self._complete_upload(adapter)

                with self.assertRaisesRegex(self._error_type(), "^outcome_unknown$") as raised:
                    adapter.verify_exact_readback(uploaded)

                self.assertEqual(len(transport.get_calls), 2)
                self.assertNotIn(provider_secret, str(raised.exception))

    def test_raising_header_and_payload_string_conversion_is_normalized(self) -> None:
        provider_secret = f"raw-provider-body {self.session_uri} {self.token}"
        initiation_transport = FakeYouTubeUploadTransport(
            post_responses=[
                FakeUploadResponse(
                    200,
                    {},
                    headers={"Location": RaisingStripString(provider_secret)},
                )
            ]
        )
        initiation_adapter = self._adapter(initiation_transport)
        with self.assertRaisesRegex(self._error_type(), "^outcome_unknown$") as raised:
            self._begin(initiation_adapter)
        self.assertNotIn(provider_secret, str(raised.exception))

        upload_transport = FakeYouTubeUploadTransport(
            post_responses=[self._session_response()],
            put_responses=[
                FakeUploadResponse(
                    201,
                    {"id": RaisingStripString(provider_secret)},
                )
            ],
        )
        upload_adapter = self._adapter(upload_transport)

        result = upload_adapter.upload_or_resume(self._begin(upload_adapter))

        self.assertEqual(result, self._receipt(state="outcome_unknown", video_id=None))
        self.assertNotIn(provider_secret, repr(result))

    def test_readback_requires_adapter_owned_upload_receipt(self) -> None:
        transport = FakeYouTubeUploadTransport(
            post_responses=[self._session_response()],
            put_responses=[FakeUploadResponse(201, {"id": "video_exact"})],
        )
        adapter = self._adapter(transport)
        uploaded = self._complete_upload(adapter)
        with self.assertRaisesRegex(self._error_type(), "^readback_mismatch$"):
            adapter.verify_exact_readback(self._receipt())

        before_upload_transport = FakeYouTubeUploadTransport()
        before_upload_adapter = self._adapter(before_upload_transport)
        with self.assertRaisesRegex(self._error_type(), "^readback_mismatch$"):
            before_upload_adapter.verify_exact_readback(self._receipt())

        self.assertEqual(len(transport.get_calls), 1)
        self.assertEqual(before_upload_transport.get_calls, [])


if __name__ == "__main__":
    unittest.main()
