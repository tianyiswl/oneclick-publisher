"""Zero-write value contracts for a future desktop YouTube API upload."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


YOUTUBE_CHANNELS_ENDPOINT = "https://www.googleapis.com/youtube/v3/channels"
CHANNEL_LOOKUP_TIMEOUT_SECONDS = 10.0


class YouTubePreflightError(Exception):
    """Local input was not safe for the constrained YouTube API route."""


class YouTubeChannelLookupError(Exception):
    """Authenticated channel identity could not be established safely."""


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

        payload = _channel_payload(response)
        if not _channel_response_is_success(response):
            raise YouTubeChannelLookupError("channel_lookup_rejected")
        items = payload.get("items")
        if not isinstance(items, list):
            raise YouTubeChannelLookupError("channel_lookup_response_invalid")
        if not items:
            raise YouTubeChannelLookupError("channel_not_found")
        if len(items) != 1:
            raise YouTubeChannelLookupError("channel_identity_ambiguous")
        item = items[0]
        if not isinstance(item, Mapping):
            raise YouTubeChannelLookupError("channel_lookup_response_invalid")
        channel_id = item.get("id")
        if not isinstance(channel_id, str) or not channel_id.strip():
            raise YouTubeChannelLookupError("channel_identity_invalid")
        return YouTubeChannelIdentity(
            channel_id=channel_id.strip(),
            display_name=_channel_display_name(item),
        )


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
        return (
            not isinstance(status_code, bool)
            and isinstance(status_code, int)
            and 200 <= status_code < 300
        )
    except Exception:
        raise YouTubeChannelLookupError("channel_lookup_response_invalid") from None


def _channel_display_name(item: Mapping[str, object]) -> str | None:
    snippet = item.get("snippet")
    if not isinstance(snippet, Mapping):
        return None
    title = snippet.get("title")
    return title if isinstance(title, str) and title else None
