"""Official YouTube publish validation and exact-video management contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PIL import Image, UnidentifiedImageError

from .overseas_youtube_api import (
    YOUTUBE_VIDEOS_ENDPOINT,
    ValidatedYouTubeUpload,
    YouTubePreflightError,
    YouTubeUploadRequest,
    local_preflight,
)


YOUTUBE_THUMBNAILS_UPLOAD_ENDPOINT = (
    "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
)
YOUTUBE_MANAGEMENT_TIMEOUT_SECONDS = 30.0
YOUTUBE_MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024
YOUTUBE_MIN_SCHEDULE_LEAD = timedelta(minutes=15)
_YOUTUBE_TARGET_VISIBILITIES = frozenset(
    {"private", "unlisted", "public", "scheduled_public"}
)
_PRESERVED_STATUS_FIELDS = frozenset(
    {"license", "embeddable", "publicStatsViewable", "containsSyntheticMedia"}
)
_THUMBNAIL_MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


class YouTubeOfficialPublishError(Exception):
    """Official publishing stopped with a stable, secret-safe reason code."""

    def __init__(
        self,
        error_code: str,
        *,
        receipt: Mapping[str, object] | None = None,
    ) -> None:
        self.error_code = str(error_code)
        self.receipt = dict(receipt or {})
        super().__init__(self.error_code)


@dataclass(frozen=True, slots=True)
class YouTubePublishSettings:
    visibility: str
    made_for_kids: bool
    notify_subscribers: bool
    publish_at: str | None
    thumbnail_path: Path | None


@dataclass(frozen=True, slots=True)
class ValidatedYouTubePublish:
    private_upload: ValidatedYouTubeUpload
    settings: YouTubePublishSettings
    account_id: int
    credential_reference: str
    expected_channel_id: str


@dataclass(frozen=True, slots=True)
class YouTubeVideoStatus:
    video_id: str
    privacy_status: str
    publish_at: str | None
    made_for_kids: bool | None
    upload_status: str | None
    processing_status: str | None
    channel_id: str | None
    title: str | None
    preserved_status: tuple[tuple[str, object], ...] = ()


class _ManagementResponse(Protocol):
    status_code: int

    def json(self) -> object: ...


class YouTubeManagementTransport(Protocol):
    def get(self, url: str, **kwargs: object) -> _ManagementResponse: ...

    def put(self, url: str, **kwargs: object) -> _ManagementResponse: ...

    def post(self, url: str, **kwargs: object) -> _ManagementResponse: ...


def validate_youtube_publish_payload(
    payload: Mapping[str, object],
    *,
    now: datetime | None = None,
) -> ValidatedYouTubePublish:
    """Validate an official target while retaining a private-only upload request."""

    if not isinstance(payload, Mapping) or int(payload.get("type") or 0) != 7:
        raise YouTubeOfficialPublishError("youtube_payload_invalid")
    if payload.get("youtubeOfficialApi") is not True:
        raise YouTubeOfficialPublishError("youtube_oauth_required")

    account_ids = payload.get("accountIds")
    if (
        not isinstance(account_ids, Sequence)
        or isinstance(account_ids, (str, bytes))
        or len(account_ids) != 1
        or type(account_ids[0]) is not int
        or account_ids[0] <= 0
    ):
        raise YouTubeOfficialPublishError("youtube_account_invalid")
    credential_references = payload.get("accountList")
    if (
        not isinstance(credential_references, Sequence)
        or isinstance(credential_references, (str, bytes))
        or len(credential_references) != 1
    ):
        raise YouTubeOfficialPublishError("youtube_account_invalid")
    credential_reference = str(credential_references[0] or "").strip()
    expected_channel_id = str(
        payload.get("youtubeExpectedChannelId") or ""
    ).strip()
    if (
        not credential_reference.startswith("youtube-oauth:")
        or not expected_channel_id
    ):
        raise YouTubeOfficialPublishError("youtube_account_invalid")

    visibility = str(payload.get("visibility") or "private").strip().lower()
    if visibility not in _YOUTUBE_TARGET_VISIBILITIES:
        raise YouTubeOfficialPublishError("youtube_visibility_invalid")
    made_for_kids = payload.get("madeForKids")
    if type(made_for_kids) is not bool:
        raise YouTubeOfficialPublishError("youtube_audience_required")
    notify_subscribers = payload.get("notifySubscribers", True)
    if type(notify_subscribers) is not bool:
        raise YouTubeOfficialPublishError("youtube_notify_invalid")

    publish_at = _validated_publish_at(payload, visibility=visibility, now=now)
    thumbnail_path = _validated_thumbnail_path(payload.get("coverPath"))
    private_upload = _validated_private_upload(
        payload,
        made_for_kids=made_for_kids,
        notify_subscribers=notify_subscribers,
    )
    return ValidatedYouTubePublish(
        private_upload=private_upload,
        settings=YouTubePublishSettings(
            visibility=visibility,
            made_for_kids=made_for_kids,
            notify_subscribers=notify_subscribers,
            publish_at=publish_at,
            thumbnail_path=thumbnail_path,
        ),
        account_id=account_ids[0],
        credential_reference=credential_reference,
        expected_channel_id=expected_channel_id,
    )


def _validated_private_upload(
    payload: Mapping[str, object],
    *,
    made_for_kids: bool,
    notify_subscribers: bool,
) -> ValidatedYouTubeUpload:
    file_list = payload.get("fileList")
    tags = payload.get("tags")
    title = payload.get("title")
    description = payload.get("description")
    if (
        not isinstance(file_list, Sequence)
        or isinstance(file_list, (str, bytes))
        or not isinstance(tags, Sequence)
        or isinstance(tags, (str, bytes))
        or not isinstance(title, str)
        or not isinstance(description, str)
    ):
        raise YouTubeOfficialPublishError("youtube_payload_invalid")
    request = YouTubeUploadRequest(
        video_paths=tuple(file_list),
        title=title,
        description=description,
        tags=tuple(tags),
        category_id=(
            str(payload.get("categoryId")).strip()
            if payload.get("categoryId") not in (None, "")
            else None
        ),
        made_for_kids=made_for_kids,
        notify_subscribers=notify_subscribers,
        visibility="private",
        scheduled_at=None,
        timer_enabled=False,
        thumbnail_path=None,
        cover_path=None,
    )
    try:
        return local_preflight(request)
    except YouTubePreflightError as exc:
        reason = str(exc)
        error_code = (
            "youtube_video_file_invalid"
            if reason in {"video_count_invalid", "video_unreadable"}
            else "youtube_payload_invalid"
        )
        raise YouTubeOfficialPublishError(error_code) from None


def _validated_publish_at(
    payload: Mapping[str, object],
    *,
    visibility: str,
    now: datetime | None,
) -> str | None:
    timer_enabled = payload.get("enableTimer", False)
    schedule_time = payload.get("scheduleTime")
    timezone_name = str(payload.get("scheduleTimezone") or "Asia/Shanghai").strip()
    if type(timer_enabled) is not bool:
        raise YouTubeOfficialPublishError("youtube_schedule_invalid")
    if visibility != "scheduled_public":
        if timer_enabled or schedule_time not in (None, ""):
            raise YouTubeOfficialPublishError("youtube_schedule_invalid")
        return None
    if timer_enabled is not True or timezone_name != "Asia/Shanghai":
        raise YouTubeOfficialPublishError("youtube_schedule_invalid")
    local_time = str(schedule_time or "").strip()
    try:
        zone = ZoneInfo(timezone_name)
        scheduled = datetime.strptime(local_time, "%Y-%m-%d %H:%M").replace(
            tzinfo=zone
        )
    except (ValueError, ZoneInfoNotFoundError):
        raise YouTubeOfficialPublishError("youtube_schedule_invalid") from None
    if scheduled.strftime("%Y-%m-%d %H:%M") != local_time:
        raise YouTubeOfficialPublishError("youtube_schedule_invalid")
    current = now or datetime.now(timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise YouTubeOfficialPublishError("youtube_schedule_invalid")
    if scheduled < current.astimezone(zone) + YOUTUBE_MIN_SCHEDULE_LEAD:
        raise YouTubeOfficialPublishError("youtube_schedule_invalid")
    return scheduled.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validated_thumbnail_path(value: object) -> Path | None:
    if value in (None, ""):
        return None
    try:
        path = Path(str(value)).expanduser().resolve(strict=True)
        size = path.stat().st_size
    except (OSError, RuntimeError, TypeError, ValueError):
        raise YouTubeOfficialPublishError("youtube_thumbnail_invalid") from None
    if (
        not path.is_file()
        or path.suffix.lower() not in _THUMBNAIL_MIME_BY_SUFFIX
        or size <= 0
        or size > YOUTUBE_MAX_THUMBNAIL_BYTES
    ):
        raise YouTubeOfficialPublishError("youtube_thumbnail_invalid")
    try:
        with Image.open(path) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > 25_000_000:
                raise YouTubeOfficialPublishError("youtube_thumbnail_invalid")
            image.verify()
            image_format = str(image.format or "").upper()
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        raise YouTubeOfficialPublishError("youtube_thumbnail_invalid") from None
    expected_formats = {"JPEG"} if path.suffix.lower() in {".jpg", ".jpeg"} else {"PNG"}
    if image_format not in expected_formats:
        raise YouTubeOfficialPublishError("youtube_thumbnail_invalid")
    return path


class YouTubeVideoManagementClient:
    """Read and update one exact video without retaining bearer credentials."""

    def __init__(
        self,
        transport: YouTubeManagementTransport,
        *,
        timeout_seconds: float = YOUTUBE_MANAGEMENT_TIMEOUT_SECONDS,
    ) -> None:
        self._transport = transport
        self._timeout_seconds = float(timeout_seconds)

    def __repr__(self) -> str:
        return "YouTubeVideoManagementClient(<transport>)"

    def read_status(
        self,
        access_token: str,
        *,
        video_id: str,
    ) -> YouTubeVideoStatus:
        token = _validated_access_token(access_token)
        exact_video_id = _validated_video_id(video_id)
        try:
            response = self._transport.get(
                YOUTUBE_VIDEOS_ENDPOINT,
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "part": "id,snippet,status,processingDetails",
                    "id": exact_video_id,
                },
                timeout=self._timeout_seconds,
                allow_redirects=False,
            )
        except Exception:
            raise YouTubeOfficialPublishError("youtube_readback_mismatch") from None
        return _parse_exact_video_status(response, video_id=exact_video_id)

    def set_thumbnail(
        self,
        access_token: str,
        *,
        video_id: str,
        path: Path,
    ) -> None:
        token = _validated_access_token(access_token)
        exact_video_id = _validated_video_id(video_id)
        checked_path = _validated_thumbnail_path(path)
        if checked_path is None:
            raise YouTubeOfficialPublishError("youtube_thumbnail_invalid")
        try:
            media = checked_path.read_bytes()
            response = self._transport.post(
                YOUTUBE_THUMBNAILS_UPLOAD_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": _THUMBNAIL_MIME_BY_SUFFIX[
                        checked_path.suffix.lower()
                    ],
                },
                params={"videoId": exact_video_id, "uploadType": "media"},
                data=media,
                timeout=self._timeout_seconds,
                allow_redirects=False,
            )
        except YouTubeOfficialPublishError:
            raise
        except Exception:
            raise YouTubeOfficialPublishError("youtube_thumbnail_forbidden") from None
        if _response_status(response) != 200:
            raise YouTubeOfficialPublishError("youtube_thumbnail_forbidden")

    def apply_visibility(
        self,
        access_token: str,
        *,
        video_id: str,
        target_visibility: str,
        publish_at: str | None,
        made_for_kids: bool,
    ) -> YouTubeVideoStatus:
        token = _validated_access_token(access_token)
        exact_video_id = _validated_video_id(video_id)
        visibility = str(target_visibility or "").strip().lower()
        if visibility not in _YOUTUBE_TARGET_VISIBILITIES or type(made_for_kids) is not bool:
            raise YouTubeOfficialPublishError("youtube_visibility_update_failed")
        if visibility == "scheduled_public":
            if not _is_rfc3339_utc(publish_at):
                raise YouTubeOfficialPublishError("youtube_visibility_update_failed")
            privacy_status = "private"
        else:
            if publish_at is not None:
                raise YouTubeOfficialPublishError("youtube_visibility_update_failed")
            privacy_status = visibility

        current = self.read_status(token, video_id=exact_video_id)
        status = dict(current.preserved_status)
        status.update(
            {
                "privacyStatus": privacy_status,
                "selfDeclaredMadeForKids": made_for_kids,
            }
        )
        if visibility == "scheduled_public":
            status["publishAt"] = publish_at
        else:
            status.pop("publishAt", None)
        try:
            response = self._transport.put(
                YOUTUBE_VIDEOS_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                params={"part": "status"},
                json={"id": exact_video_id, "status": status},
                timeout=self._timeout_seconds,
                allow_redirects=False,
            )
        except Exception:
            raise YouTubeOfficialPublishError(
                "youtube_visibility_update_failed"
            ) from None
        if _response_status(response) != 200:
            raise YouTubeOfficialPublishError("youtube_visibility_update_failed")
        try:
            return _parse_exact_video_status(response, video_id=exact_video_id)
        except YouTubeOfficialPublishError:
            raise YouTubeOfficialPublishError(
                "youtube_visibility_update_failed"
            ) from None


def _validated_access_token(value: object) -> str:
    token = value if isinstance(value, str) else ""
    if not token or "\r" in token or "\n" in token:
        raise YouTubeOfficialPublishError("credential_unavailable")
    return token


def _is_rfc3339_utc(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ") == value


def _validated_video_id(value: object) -> str:
    video_id = value if isinstance(value, str) else ""
    if not video_id or len(video_id) > 128 or any(char.isspace() for char in video_id):
        raise YouTubeOfficialPublishError("youtube_readback_mismatch")
    return video_id


def _response_status(response: _ManagementResponse) -> int | None:
    try:
        status_code = response.status_code
    except Exception:
        return None
    return status_code if type(status_code) is int else None


def _parse_exact_video_status(
    response: _ManagementResponse,
    *,
    video_id: str,
) -> YouTubeVideoStatus:
    if _response_status(response) != 200:
        raise YouTubeOfficialPublishError("youtube_readback_mismatch")
    try:
        payload = response.json()
    except Exception:
        raise YouTubeOfficialPublishError("youtube_readback_mismatch") from None
    if not isinstance(payload, Mapping):
        raise YouTubeOfficialPublishError("youtube_readback_mismatch")
    items = payload.get("items")
    if not isinstance(items, list) or len(items) != 1:
        raise YouTubeOfficialPublishError("youtube_readback_mismatch")
    item = items[0]
    if not isinstance(item, Mapping) or item.get("id") != video_id:
        raise YouTubeOfficialPublishError("youtube_readback_mismatch")
    status = item.get("status")
    if not isinstance(status, Mapping):
        raise YouTubeOfficialPublishError("youtube_readback_mismatch")
    privacy_status = status.get("privacyStatus")
    if privacy_status not in {"private", "unlisted", "public"}:
        raise YouTubeOfficialPublishError("youtube_readback_mismatch")
    publish_at = status.get("publishAt")
    if publish_at is not None and not isinstance(publish_at, str):
        raise YouTubeOfficialPublishError("youtube_readback_mismatch")
    made_for_kids = status.get("selfDeclaredMadeForKids")
    if made_for_kids is not None and type(made_for_kids) is not bool:
        raise YouTubeOfficialPublishError("youtube_readback_mismatch")
    snippet = item.get("snippet")
    details = item.get("processingDetails")
    channel_id = snippet.get("channelId") if isinstance(snippet, Mapping) else None
    title = snippet.get("title") if isinstance(snippet, Mapping) else None
    processing_status = (
        details.get("processingStatus") if isinstance(details, Mapping) else None
    )
    upload_status = status.get("uploadStatus")
    preserved = tuple(
        sorted(
            (
                (key, status[key])
                for key in _PRESERVED_STATUS_FIELDS
                if key in status and type(status[key]) in {str, bool}
            ),
            key=lambda item: item[0],
        )
    )
    return YouTubeVideoStatus(
        video_id=video_id,
        privacy_status=privacy_status,
        publish_at=publish_at,
        made_for_kids=made_for_kids,
        upload_status=upload_status if isinstance(upload_status, str) else None,
        processing_status=(
            processing_status if isinstance(processing_status, str) else None
        ),
        channel_id=channel_id if isinstance(channel_id, str) else None,
        title=title if isinstance(title, str) else None,
        preserved_status=preserved,
    )
