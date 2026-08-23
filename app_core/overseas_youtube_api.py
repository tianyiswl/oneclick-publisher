"""Zero-write value contracts for a future desktop YouTube API upload."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Protocol


YOUTUBE_CHANNELS_ENDPOINT = "https://www.googleapis.com/youtube/v3/channels"
YOUTUBE_UPLOADS_ENDPOINT = "https://www.googleapis.com/upload/youtube/v3/videos"
YOUTUBE_VIDEOS_ENDPOINT = "https://www.googleapis.com/youtube/v3/videos"
CHANNEL_LOOKUP_TIMEOUT_SECONDS = 10.0
UPLOAD_TIMEOUT_SECONDS = 30.0
_PRIVATE_RECEIPT_STATES = frozenset(
    {
        "upload_started",
        "uploaded_private",
        "processing",
        "processed_private",
        "outcome_unknown",
    }
)


class YouTubePreflightError(Exception):
    """Local input was not safe for the constrained YouTube API route."""


class YouTubeChannelLookupError(Exception):
    """Authenticated channel identity could not be established safely."""


class YouTubeUploadError(Exception):
    """A private upload or exact-ID readback stopped with a stable reason."""


@dataclass(frozen=True, slots=True)
class YouTubeUploadRequest:
    """User-selected upload fields before local validation.

    Unsupported API-route fields are retained only long enough for preflight to
    reject them explicitly; a ``ValidatedYouTubeUpload`` never contains them.
    """

    video_paths: tuple[Path | str, ...]
    title: str
    description: str
    tags: tuple[str, ...] = ()
    category_id: str | None = None
    made_for_kids: bool = False
    notify_subscribers: bool = True
    visibility: str = "private"
    scheduled_at: object | None = None
    timer_enabled: bool = False
    playlist_id: str | None = None
    collection_id: str | None = None
    thumbnail_path: Path | str | None = None
    cover_path: Path | str | None = None
    ai_declaration: bool | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "video_paths", tuple(self.video_paths))
        object.__setattr__(self, "tags", tuple(self.tags))


@dataclass(frozen=True, slots=True)
class ValidatedYouTubeUpload:
    """The sole video and fields Task 4 may submit without reinterpreting them."""

    video_path: Path
    title: str
    description: str
    tags: tuple[str, ...]
    category_id: str | None
    made_for_kids: bool
    notify_subscribers: bool
    visibility: str = "private"

    def metadata(self) -> dict[str, object]:
        """Return the API metadata body for this already-validated request."""
        snippet: dict[str, object] = {
            "title": self.title,
            "description": self.description,
            "tags": list(self.tags),
        }
        if self.category_id is not None:
            snippet["categoryId"] = self.category_id
        return {
            "snippet": snippet,
            "status": {
                "privacyStatus": self.visibility,
                "selfDeclaredMadeForKids": self.made_for_kids,
            },
        }

    def upload_parameters(self) -> dict[str, bool]:
        """Return the non-body parameter for a future explicit upload step."""
        return {"notifySubscribers": self.notify_subscribers}


@dataclass(frozen=True, slots=True)
class YouTubeChannelIdentity:
    """Public, stable identity of the one authenticated YouTube channel."""

    channel_id: str
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class YouTubeUploadReceipt:
    """Public private-upload state; never contains credentials or session URI."""

    state: str
    video_id: str | None

    def __post_init__(self) -> None:
        if self.state not in _PRIVATE_RECEIPT_STATES:
            raise ValueError("upload_state_invalid")
        if self.state in {"uploaded_private", "processing", "processed_private"}:
            if _normalized_nonempty_string(self.video_id) is None:
                raise ValueError("video_id_required")
        elif self.video_id is not None:
            raise ValueError("video_id_unexpected")


@dataclass(frozen=True, slots=True, repr=False)
class YouTubeResumableSession:
    """Opaque immutable handle with no media routing authority."""

    _correlation: object

    def __repr__(self) -> str:
        return "YouTubeResumableSession(<redacted>)"


@dataclass(slots=True)
class _ResumableUploadState:
    """Adapter-owned routing, offset, contract, and terminal state."""

    correlation: object
    session_uri: str
    upload: ValidatedYouTubeUpload
    expected_channel: YouTubeChannelIdentity
    total_size: int
    next_byte: int = 0
    closed: bool = False
    outcome_unknown: bool = False


class _ChannelResponse(Protocol):
    status_code: int

    def json(self) -> object:
        """Return decoded response data."""


class YouTubeReadOnlyTransport(Protocol):
    """Injected read-only boundary for authenticated channel lookup."""

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str],
        timeout: float,
    ) -> _ChannelResponse:
        """Read an authenticated YouTube resource."""


class _UploadResponse(_ChannelResponse, Protocol):
    headers: Mapping[str, str]


class YouTubeUploadTransport(Protocol):
    """Injected HTTP boundary for one private resumable upload."""

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, object],
        json: Mapping[str, object],
        timeout: float,
    ) -> _UploadResponse:
        """Initiate one resumable videos.insert session."""

    def put(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        data: bytes,
        timeout: float,
    ) -> _UploadResponse:
        """Upload or resume media on the current session URI."""

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str],
        timeout: float,
    ) -> _UploadResponse:
        """Read back one exact uploaded video ID."""


class YouTubeChannelIdentityClient:
    """Find one authenticated channel through an injected GET-only transport."""

    def __init__(
        self,
        transport: YouTubeReadOnlyTransport,
        *,
        timeout_seconds: float = CHANNEL_LOOKUP_TIMEOUT_SECONDS,
    ) -> None:
        self._transport = transport
        self._timeout_seconds = timeout_seconds

    def lookup_authenticated_channel(self, access_token: str) -> YouTubeChannelIdentity:
        """Return exactly one channel identity without retaining the access token."""
        if not isinstance(access_token, str) or not access_token:
            raise YouTubeChannelLookupError("credential_unavailable")
        try:
            response = self._transport.get(
                YOUTUBE_CHANNELS_ENDPOINT,
                headers={"Authorization": f"Bearer {access_token}"},
                params={"part": "id,snippet", "mine": "true"},
                timeout=self._timeout_seconds,
            )
        except Exception:
            raise YouTubeChannelLookupError("channel_lookup_unavailable") from None

        if not _channel_response_is_success(response):
            raise YouTubeChannelLookupError("channel_lookup_rejected")
        payload = _channel_payload(response)
        payload_ok, items = _mapping_value(payload, "items")
        if not payload_ok:
            raise YouTubeChannelLookupError("channel_lookup_response_invalid")
        if not isinstance(items, list):
            raise YouTubeChannelLookupError("channel_lookup_response_invalid")
        if not items:
            raise YouTubeChannelLookupError("channel_not_found")
        if len(items) != 1:
            raise YouTubeChannelLookupError("channel_identity_ambiguous")
        item = items[0]
        if not isinstance(item, Mapping):
            raise YouTubeChannelLookupError("channel_lookup_response_invalid")
        item_ok, channel_id = _mapping_value(item, "id")
        normalized_channel_id = _normalized_nonempty_string(channel_id)
        if not item_ok or normalized_channel_id is None:
            raise YouTubeChannelLookupError("channel_identity_invalid")
        return YouTubeChannelIdentity(
            channel_id=normalized_channel_id,
            display_name=_channel_display_name(item),
        )


class YouTubePrivateUploadAdapter:
    """Perform one confirmed private resumable upload through injected HTTP."""

    def __init__(
        self,
        transport: YouTubeUploadTransport,
        *,
        timeout_seconds: float = UPLOAD_TIMEOUT_SECONDS,
    ) -> None:
        self._transport = transport
        self._timeout_seconds = timeout_seconds
        self._active_session: YouTubeResumableSession | None = None
        self._session_state: _ResumableUploadState | None = None
        self._initiation_outcome_unknown = False
        self._owned_receipt: YouTubeUploadReceipt | None = None

    def initiate_resumable_upload(
        self,
        upload: ValidatedYouTubeUpload,
        expected_channel: YouTubeChannelIdentity,
        access_token: str,
        *,
        confirmed_current: bool,
    ) -> YouTubeResumableSession:
        """Create exactly one confirmed resumable session for a private upload."""
        if confirmed_current is not True:
            raise YouTubeUploadError("authorization_denied")
        if not isinstance(upload, ValidatedYouTubeUpload) or not isinstance(
            expected_channel, YouTubeChannelIdentity
        ):
            raise YouTubeUploadError("authorization_invalid")
        if (
            upload.visibility != "private"
            or not isinstance(expected_channel.channel_id, str)
            or not expected_channel.channel_id.strip()
        ):
            raise YouTubeUploadError("authorization_invalid")
        _require_upload_token(access_token)
        if self._initiation_outcome_unknown:
            raise YouTubeUploadError("outcome_unknown")
        if self._active_session is not None:
            raise YouTubeUploadError("upload_session_exists")
        try:
            total_size = upload.video_path.stat().st_size
            if total_size < 0:
                raise OSError
        except (OSError, TypeError, ValueError):
            raise YouTubeUploadError("upload_failed") from None

        try:
            response = self._transport.post(
                YOUTUBE_UPLOADS_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "X-Upload-Content-Length": str(total_size),
                    "X-Upload-Content-Type": "application/octet-stream",
                },
                params={
                    "uploadType": "resumable",
                    "part": "snippet,status",
                    **upload.upload_parameters(),
                },
                json=upload.metadata(),
                timeout=self._timeout_seconds,
            )
        except Exception:
            self._initiation_outcome_unknown = True
            raise YouTubeUploadError("outcome_unknown") from None

        status_code = _upload_response_status(response)
        if status_code is None:
            self._initiation_outcome_unknown = True
            raise YouTubeUploadError("outcome_unknown")
        if 400 <= status_code < 500:
            raise YouTubeUploadError("upload_failed")
        if not 200 <= status_code < 300:
            self._initiation_outcome_unknown = True
            raise YouTubeUploadError("outcome_unknown")
        headers = _upload_response_headers(response)
        header_ok, location = _header_value(headers, "Location")
        normalized_location = _normalized_nonempty_string(location)
        if not header_ok or normalized_location is None:
            self._initiation_outcome_unknown = True
            raise YouTubeUploadError("outcome_unknown")

        correlation = object()
        session = YouTubeResumableSession(correlation)
        self._session_state = _ResumableUploadState(
            correlation=correlation,
            session_uri=normalized_location,
            upload=upload,
            expected_channel=expected_channel,
            total_size=total_size,
        )
        self._active_session = session
        return session

    def upload_or_resume(
        self,
        session: YouTubeResumableSession,
        access_token: str,
    ) -> YouTubeUploadReceipt:
        """Send remaining bytes only to this adapter's current session URI."""
        _require_upload_token(access_token)
        state = self._session_state
        if session is not self._active_session or state is None:
            raise YouTubeUploadError("upload_session_invalid")
        try:
            correlation_matches = session._correlation is state.correlation
        except Exception:
            correlation_matches = False
        if not correlation_matches or state.closed:
            raise YouTubeUploadError("upload_session_invalid")
        if state.outcome_unknown:
            raise YouTubeUploadError("outcome_unknown")
        try:
            with state.upload.video_path.open("rb") as video_file:
                current_size = state.upload.video_path.stat().st_size
                if current_size != state.total_size:
                    raise OSError
                video_file.seek(state.next_byte)
                media = video_file.read()
        except (OSError, TypeError, ValueError):
            raise YouTubeUploadError("upload_failed") from None

        if state.next_byte < state.total_size:
            content_range = (
                f"bytes {state.next_byte}-{state.total_size - 1}/"
                f"{state.total_size}"
            )
        else:
            content_range = f"bytes */{state.total_size}"
        try:
            response = self._transport.put(
                state.session_uri,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Length": str(len(media)),
                    "Content-Type": "application/octet-stream",
                    "Content-Range": content_range,
                },
                data=media,
                timeout=self._timeout_seconds,
            )
        except Exception:
            return self._mark_upload_outcome_unknown()

        status_code = _upload_response_status(response)
        if status_code is None:
            return self._mark_upload_outcome_unknown()
        if status_code == 308:
            next_byte = _resumable_next_byte(response, state.total_size)
            if next_byte is None:
                return self._mark_upload_outcome_unknown()
            state.next_byte = next_byte
            return YouTubeUploadReceipt(state="upload_started", video_id=None)
        if status_code == 201:
            payload = _upload_payload(response)
            payload_ok, video_id = _mapping_value(payload, "id")
            normalized_video_id = _normalized_nonempty_string(video_id)
            if not payload_ok or normalized_video_id is None:
                return self._mark_upload_outcome_unknown()
            state.closed = True
            receipt = YouTubeUploadReceipt(
                state="uploaded_private",
                video_id=normalized_video_id,
            )
            self._owned_receipt = receipt
            return receipt
        if 400 <= status_code < 500:
            raise YouTubeUploadError("upload_failed")
        return self._mark_upload_outcome_unknown()

    def _mark_upload_outcome_unknown(self) -> YouTubeUploadReceipt:
        if self._session_state is not None:
            self._session_state.outcome_unknown = True
        return YouTubeUploadReceipt(state="outcome_unknown", video_id=None)

    def verify_exact_readback(
        self,
        receipt: YouTubeUploadReceipt,
        upload: ValidatedYouTubeUpload,
        expected_channel: YouTubeChannelIdentity,
        access_token: str,
    ) -> YouTubeUploadReceipt:
        """Verify one exact uploaded ID and report only private saved states."""
        state = self._session_state
        if (
            state is None
            or receipt is not self._owned_receipt
            or upload is not state.upload
            or expected_channel is not state.expected_channel
        ):
            raise YouTubeUploadError("readback_mismatch")
        if (
            not isinstance(receipt, YouTubeUploadReceipt)
            or receipt.state not in {"uploaded_private", "processing", "processed_private"}
            or not isinstance(receipt.video_id, str)
            or not receipt.video_id
            or not isinstance(upload, ValidatedYouTubeUpload)
            or not isinstance(expected_channel, YouTubeChannelIdentity)
            or upload.visibility != "private"
        ):
            raise YouTubeUploadError("authorization_invalid")
        _require_upload_token(access_token)
        try:
            response = self._transport.get(
                YOUTUBE_VIDEOS_ENDPOINT,
                headers={"Authorization": f"Bearer {access_token}"},
                params={
                    "part": "id,snippet,status,processingDetails",
                    "id": receipt.video_id,
                },
                timeout=self._timeout_seconds,
            )
        except Exception:
            raise YouTubeUploadError("outcome_unknown") from None

        if _upload_response_status(response) != 200:
            raise YouTubeUploadError("outcome_unknown")
        payload = _upload_payload(response)
        if payload is None:
            raise YouTubeUploadError("outcome_unknown")
        payload_ok, items = _mapping_value(payload, "items")
        if not payload_ok:
            raise YouTubeUploadError("outcome_unknown")
        if not isinstance(items, list) or len(items) != 1:
            raise YouTubeUploadError("readback_mismatch")
        item = items[0]
        if not isinstance(item, Mapping):
            raise YouTubeUploadError("readback_mismatch")
        snippet_ok, snippet = _mapping_value(item, "snippet")
        status_ok, status = _mapping_value(item, "status")
        if not snippet_ok or not status_ok:
            raise YouTubeUploadError("outcome_unknown")
        if not isinstance(snippet, Mapping) or not isinstance(status, Mapping):
            raise YouTubeUploadError("readback_mismatch")
        item_id_ok, item_id = _mapping_value(item, "id")
        channel_ok, channel_id = _mapping_value(snippet, "channelId")
        title_ok, title = _mapping_value(snippet, "title")
        privacy_ok, privacy_status = _mapping_value(status, "privacyStatus")
        if not all((item_id_ok, channel_ok, title_ok, privacy_ok)):
            raise YouTubeUploadError("outcome_unknown")
        if (
            item_id != receipt.video_id
            or channel_id != state.expected_channel.channel_id
            or title != state.upload.title
            or privacy_status != "private"
        ):
            raise YouTubeUploadError("readback_mismatch")

        state = _private_processing_state(item, status)
        verified_receipt = YouTubeUploadReceipt(state=state, video_id=receipt.video_id)
        self._owned_receipt = verified_receipt
        return verified_receipt


def local_preflight(request: YouTubeUploadRequest) -> ValidatedYouTubeUpload:
    """Validate one private local upload without making any platform request."""
    if not isinstance(request, YouTubeUploadRequest):
        raise YouTubePreflightError("request_invalid")
    if len(request.video_paths) != 1:
        raise YouTubePreflightError("video_count_invalid")
    video_path = _readable_local_file(request.video_paths[0])
    if video_path is None:
        raise YouTubePreflightError("video_unreadable")
    _validate_supported_fields(request)
    return ValidatedYouTubeUpload(
        video_path=video_path,
        title=request.title,
        description=request.description,
        tags=request.tags,
        category_id=request.category_id,
        made_for_kids=request.made_for_kids,
        notify_subscribers=request.notify_subscribers,
    )


def _validate_supported_fields(request: YouTubeUploadRequest) -> None:
    if not isinstance(request.title, str) or not request.title.strip():
        raise YouTubePreflightError("title_required")
    if len(request.title) > 100:
        raise YouTubePreflightError("title_too_long")
    if not isinstance(request.description, str):
        raise YouTubePreflightError("description_invalid")
    try:
        description_size = len(request.description.encode("utf-8"))
    except UnicodeError:
        raise YouTubePreflightError("description_invalid") from None
    if description_size > 5000:
        raise YouTubePreflightError("description_too_long")
    if not isinstance(request.visibility, str) or request.visibility.lower() != "private":
        raise YouTubePreflightError("visibility_private_required")
    if request.scheduled_at is not None or request.timer_enabled is True:
        raise YouTubePreflightError("scheduling_unsupported")
    if request.playlist_id is not None or request.collection_id is not None:
        raise YouTubePreflightError("playlist_unsupported")
    if request.thumbnail_path is not None or request.cover_path is not None:
        raise YouTubePreflightError("thumbnail_unsupported")
    if request.ai_declaration is not None:
        raise YouTubePreflightError("ai_declaration_unsupported")
    if not isinstance(request.timer_enabled, bool):
        raise YouTubePreflightError("timer_invalid")
    if not isinstance(request.made_for_kids, bool):
        raise YouTubePreflightError("made_for_kids_invalid")
    if not isinstance(request.notify_subscribers, bool):
        raise YouTubePreflightError("notify_subscribers_invalid")
    if not isinstance(request.tags, tuple) or any(
        not isinstance(tag, str) or not tag.strip() for tag in request.tags
    ):
        raise YouTubePreflightError("tags_invalid")
    if request.category_id is not None and (
        not isinstance(request.category_id, str) or not request.category_id.strip()
    ):
        raise YouTubePreflightError("category_invalid")


def _readable_local_file(value: Path | str) -> Path | None:
    try:
        path = Path(value)
        if not path.is_file():
            return None
        with path.open("rb"):
            pass
        return path
    except (OSError, TypeError, ValueError):
        return None


def _require_upload_token(access_token: str) -> None:
    if not isinstance(access_token, str) or not access_token:
        raise YouTubeUploadError("credential_unavailable")


def _upload_response_status(response: object) -> int | None:
    try:
        status_code = response.status_code  # type: ignore[attr-defined]
    except Exception:
        return None
    if isinstance(status_code, bool) or not isinstance(status_code, int):
        return None
    return status_code


def _upload_response_headers(response: object) -> Mapping[str, object] | None:
    try:
        headers = response.headers  # type: ignore[attr-defined]
    except Exception:
        return None
    return headers if isinstance(headers, Mapping) else None


def _header_value(
    headers: Mapping[str, object] | None,
    name: str,
) -> tuple[bool, str | None]:
    if headers is None:
        return False, None
    try:
        for key, value in headers.items():
            if isinstance(key, str) and key.lower() == name.lower():
                return True, value if isinstance(value, str) else None
    except Exception:
        return False, None
    return True, None


def _mapping_value(
    mapping: Mapping[str, object] | None,
    key: str,
) -> tuple[bool, object]:
    if mapping is None:
        return False, None
    try:
        return True, mapping.get(key)
    except Exception:
        return False, None


def _normalized_nonempty_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        normalized = value.strip()
    except Exception:
        return None
    return normalized if normalized else None


def _upload_payload(response: object) -> Mapping[str, object] | None:
    try:
        payload = response.json()  # type: ignore[attr-defined]
    except Exception:
        return None
    return payload if isinstance(payload, Mapping) else None


def _resumable_next_byte(response: object, total_size: int) -> int | None:
    headers = _upload_response_headers(response)
    if headers is None:
        return None
    header_ok, received_range = _header_value(headers, "Range")
    if not header_ok:
        return None
    if received_range is None:
        return 0
    normalized_range = _normalized_nonempty_string(received_range)
    if normalized_range is None:
        return None
    match = re.fullmatch(r"bytes=0-(\d+)", normalized_range)
    if match is None:
        return None
    try:
        next_byte = int(match.group(1)) + 1
        outside_media = next_byte < 0 or next_byte > total_size
    except Exception:
        return None
    if outside_media:
        return None
    return next_byte


def _private_processing_state(
    item: Mapping[str, object],
    status: Mapping[str, object],
) -> str:
    upload_ok, upload_status = _mapping_value(status, "uploadStatus")
    rejection_ok, rejection_reason = _mapping_value(status, "rejectionReason")
    details_ok, processing_details = _mapping_value(item, "processingDetails")
    if not all((upload_ok, rejection_ok, details_ok)):
        raise YouTubeUploadError("outcome_unknown")
    processing_status: object = None
    if isinstance(processing_details, Mapping):
        processing_ok, processing_status = _mapping_value(
            processing_details,
            "processingStatus",
        )
        if not processing_ok:
            raise YouTubeUploadError("outcome_unknown")
    if upload_status == "rejected" or (
        isinstance(rejection_reason, str) and rejection_reason
    ):
        raise YouTubeUploadError("rejected")
    if upload_status in {"failed", "deleted"} or processing_status in {
        "failed",
        "terminated",
    }:
        raise YouTubeUploadError("processing_failed")
    if processing_status == "processing" or upload_status == "uploaded":
        return "processing"
    if processing_status == "succeeded" or upload_status == "processed":
        return "processed_private"
    raise YouTubeUploadError("outcome_unknown")


def _channel_payload(response: _ChannelResponse) -> Mapping[str, object]:
    try:
        payload = response.json()
    except Exception:
        raise YouTubeChannelLookupError("channel_lookup_response_invalid") from None
    if not isinstance(payload, Mapping):
        raise YouTubeChannelLookupError("channel_lookup_response_invalid")
    return payload


def _channel_response_is_success(response: _ChannelResponse) -> bool:
    try:
        status_code = response.status_code
        if isinstance(status_code, bool) or not isinstance(status_code, int):
            raise TypeError
        return 200 <= status_code < 300
    except Exception:
        raise YouTubeChannelLookupError("channel_lookup_response_invalid") from None


def _channel_display_name(item: Mapping[str, object]) -> str | None:
    snippet_ok, snippet = _mapping_value(item, "snippet")
    if not snippet_ok or not isinstance(snippet, Mapping):
        return None
    title_ok, title = _mapping_value(snippet, "title")
    return title if title_ok and isinstance(title, str) and title else None
