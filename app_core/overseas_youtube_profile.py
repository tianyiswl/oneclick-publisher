"""Safe refresh helpers for public YouTube OAuth channel profile data."""

from __future__ import annotations

from io import BytesIO
import os
from pathlib import Path
import re
from typing import Mapping, Protocol
from urllib.parse import urljoin, urlsplit

from PIL import Image, UnidentifiedImageError
import requests

from conf import YOUTUBE_OAUTH_CLIENT_ID

from .overseas_youtube_login import validate_saved_youtube_oauth_account
from .paths import AVATAR_DIR


_APPROVED_AVATAR_HOSTS = frozenset(
    {
        "yt3.ggpht.com",
        "yt3.googleusercontent.com",
        "lh3.googleusercontent.com",
    }
)
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
_CHANNEL_ID_PATTERN = re.compile(r"UC[A-Za-z0-9_-]+\Z")
MAX_AVATAR_BYTES = 5 * 1024 * 1024
MAX_AVATAR_REDIRECTS = 3
MAX_AVATAR_PIXELS = 25_000_000
AVATAR_TIMEOUT_SECONDS = 15.0


class YouTubeProfileError(RuntimeError):
    """A public-profile operation stopped with a stable, secret-free code."""


class _AvatarResponse(Protocol):
    status_code: int
    headers: Mapping[str, object]

    def iter_content(self, chunk_size: int): ...

    def close(self) -> None: ...


class _AvatarTransport(Protocol):
    def get(
        self,
        url: str,
        *,
        timeout: float,
        stream: bool,
        allow_redirects: bool,
    ) -> _AvatarResponse: ...


def youtube_studio_url(account: Mapping[str, object]) -> str:
    """Build a Studio URL from the stored public channel ID only."""

    channel_id = str(account.get("accountReference") or "").strip()
    if _CHANNEL_ID_PATTERN.fullmatch(channel_id):
        return f"https://studio.youtube.com/channel/{channel_id}"
    return "https://studio.youtube.com/"


def _validated_avatar_url(raw_url: object) -> str:
    if not isinstance(raw_url, str) or not raw_url:
        raise YouTubeProfileError("youtube_avatar_url_invalid")
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except (TypeError, ValueError):
        raise YouTubeProfileError("youtube_avatar_url_invalid") from None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.hostname.lower() not in _APPROVED_AVATAR_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise YouTubeProfileError("youtube_avatar_url_invalid")
    return raw_url


def _response_header(response: _AvatarResponse, name: str) -> str:
    try:
        headers = response.headers
        value = headers.get(name) or headers.get(name.lower())
    except Exception:
        return ""
    return value.strip() if isinstance(value, str) else ""


def _response_status(response: _AvatarResponse) -> int:
    try:
        status = response.status_code
    except Exception:
        raise YouTubeProfileError("youtube_avatar_download_failed") from None
    if type(status) is not int:
        raise YouTubeProfileError("youtube_avatar_download_failed")
    return status


def _bounded_response_bytes(response: _AvatarResponse) -> bytes:
    declared = _response_header(response, "Content-Length")
    if declared:
        try:
            declared_size = int(declared)
        except ValueError:
            raise YouTubeProfileError("youtube_avatar_download_failed") from None
        if declared_size < 0:
            raise YouTubeProfileError("youtube_avatar_download_failed")
        if declared_size > MAX_AVATAR_BYTES:
            raise YouTubeProfileError("youtube_avatar_too_large")

    collected = bytearray()
    try:
        chunks = response.iter_content(chunk_size=64 * 1024)
        for chunk in chunks:
            if not isinstance(chunk, bytes):
                raise YouTubeProfileError("youtube_avatar_download_failed")
            if len(collected) + len(chunk) > MAX_AVATAR_BYTES:
                raise YouTubeProfileError("youtube_avatar_too_large")
            collected.extend(chunk)
    except YouTubeProfileError:
        raise
    except Exception:
        raise YouTubeProfileError("youtube_avatar_download_failed") from None
    if not collected:
        raise YouTubeProfileError("youtube_avatar_invalid")
    return bytes(collected)


def _normalized_png(raw_image: bytes) -> bytes:
    try:
        with Image.open(BytesIO(raw_image)) as decoded:
            width, height = decoded.size
            if (
                width <= 0
                or height <= 0
                or width * height > MAX_AVATAR_PIXELS
            ):
                raise YouTubeProfileError("youtube_avatar_invalid")
            decoded.load()
            normalized = decoded.convert("RGBA")
        output = BytesIO()
        normalized.save(output, format="PNG", optimize=True)
        return output.getvalue()
    except YouTubeProfileError:
        raise
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError):
        raise YouTubeProfileError("youtube_avatar_invalid") from None


class YouTubeAvatarDownloader:
    """Download one approved public avatar and replace its local PNG atomically."""

    def __init__(
        self,
        transport: _AvatarTransport | None = None,
        *,
        timeout_seconds: float = AVATAR_TIMEOUT_SECONDS,
    ) -> None:
        self._transport = transport or requests.Session()
        self._timeout_seconds = float(timeout_seconds)

    def download(
        self,
        url: str,
        *,
        account_id: int,
        avatar_dir: Path = AVATAR_DIR,
    ) -> str:
        account_id = int(account_id)
        if account_id <= 0:
            raise YouTubeProfileError("youtube_profile_account_invalid")
        current_url = _validated_avatar_url(url)
        raw_image: bytes | None = None

        for redirect_count in range(MAX_AVATAR_REDIRECTS + 1):
            response: _AvatarResponse | None = None
            try:
                response = self._transport.get(
                    current_url,
                    timeout=self._timeout_seconds,
                    stream=True,
                    allow_redirects=False,
                )
                status = _response_status(response)
                if status in _REDIRECT_STATUS_CODES:
                    if redirect_count >= MAX_AVATAR_REDIRECTS:
                        raise YouTubeProfileError("youtube_avatar_redirect_limit")
                    location = _response_header(response, "Location")
                    if not location:
                        raise YouTubeProfileError("youtube_avatar_download_failed")
                    current_url = _validated_avatar_url(
                        urljoin(current_url, location)
                    )
                    continue
                if status != 200:
                    raise YouTubeProfileError("youtube_avatar_download_failed")
                raw_image = _bounded_response_bytes(response)
                break
            except YouTubeProfileError:
                raise
            except Exception:
                raise YouTubeProfileError("youtube_avatar_download_failed") from None
            finally:
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass

        if raw_image is None:
            raise YouTubeProfileError("youtube_avatar_download_failed")
        png_bytes = _normalized_png(raw_image)
        directory = Path(avatar_dir)
        file_name = f"oneclick_account_{account_id}.png"
        destination = directory / file_name
        temporary = directory / f".{file_name}.tmp"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with temporary.open("wb") as handle:
                handle.write(png_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise YouTubeProfileError("youtube_avatar_write_failed") from None
        return file_name


def refresh_youtube_oauth_profile(
    account: Mapping[str, object],
    *,
    avatar_dir: Path = AVATAR_DIR,
    downloader: YouTubeAvatarDownloader | None = None,
) -> dict[str, str]:
    """Refresh one saved OAuth row without returning or persisting remote URLs."""

    if (
        not isinstance(account, Mapping)
        or int(account.get("id") or 0) <= 0
        or int(account.get("type") or 0) != 7
        or str(account.get("authMode") or "") != "youtube_oauth"
    ):
        raise YouTubeProfileError("youtube_profile_account_invalid")
    account_id = int(account["id"])
    expected_channel_id = str(account.get("accountReference") or "").strip()
    if not _CHANNEL_ID_PATTERN.fullmatch(expected_channel_id):
        raise YouTubeProfileError("youtube_profile_account_invalid")
    try:
        identity = validate_saved_youtube_oauth_account(
            account,
            client_id=YOUTUBE_OAUTH_CLIENT_ID,
        )
    except Exception as exc:
        reason = str(exc)
        safe_reasons = {
            "youtube_oauth_client_not_configured",
            "youtube_oauth_client_secret_not_configured",
            "credential_unavailable",
            "authorization_invalid",
            "channel_identity_mismatch",
            "channel_identity_unavailable",
        }
        raise YouTubeProfileError(
            reason if reason in safe_reasons else "youtube_profile_refresh_failed"
        ) from None
    if identity.channel_id != expected_channel_id:
        raise YouTubeProfileError("youtube_channel_identity_mismatch")
    if not identity.avatar_url:
        raise YouTubeProfileError("youtube_avatar_unavailable")

    file_name = (downloader or YouTubeAvatarDownloader()).download(
        identity.avatar_url,
        account_id=account_id,
        avatar_dir=Path(avatar_dir),
    )
    display_name = str(identity.display_name or "").strip()
    if not display_name:
        display_name = str(account.get("userName") or "YouTube 频道").strip()
    return {
        "displayName": display_name,
        "avatarFileName": Path(file_name).name,
    }
