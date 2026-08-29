# -*- coding: utf-8 -*-
"""Read and persist public TikTok profile presentation fields safely."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping
from urllib.parse import urljoin, urlsplit

from PIL import Image, UnidentifiedImageError
import requests

from .database import connect
from .overseas_tiktok_identity import (
    TikTokIdentity,
    TikTokIdentityError,
    normalize_tiktok_handle,
)
from .paths import AVATAR_DIR


TIKTOK_PROFILE_NAVIGATION_TIMEOUT_MS = 120_000
TIKTOK_PROFILE_MAX_ATTEMPTS = 150
TIKTOK_PROFILE_POLL_SECONDS = 0.5
MAX_AVATAR_BYTES = 5 * 1024 * 1024
MAX_AVATAR_PIXELS = 25_000_000
MAX_PROFILE_HTML_BYTES = 5 * 1024 * 1024
MAX_AVATAR_REDIRECTS = 3
PUBLIC_PROFILE_TIMEOUT_SECONDS = 45.0
_PUBLIC_PROFILE_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
_APPROVED_AVATAR_HOST_SUFFIXES = (
    "tiktokcdn.com",
    "tiktokcdn-us.com",
    "tiktokcdn-eu.com",
    "byteimg.com",
    "muscdn.com",
)
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
_PROFILE_AVATAR_SELECTOR = '[data-e2e="user-avatar"] img'
_PROFILE_PUBLIC_DATA_EVALUATOR = """
() => {
  const script = document.querySelector('script#__UNIVERSAL_DATA_FOR_REHYDRATION__');
  if (!script) return { state: 'missing' };
  try {
    const documentData = JSON.parse(script.textContent || '');
    const scope = documentData && documentData.__DEFAULT_SCOPE__;
    const detail = scope && scope['webapp.user-detail'];
    const user = detail && detail.userInfo && detail.userInfo.user;
    if (!user || typeof user !== 'object') return { state: 'missing' };
    return {
      state: 'ok',
      uniqueId: typeof user.uniqueId === 'string' ? user.uniqueId : '',
      nickname: typeof user.nickname === 'string' ? user.nickname : ''
    };
  } catch (_error) {
    return { state: 'invalid' };
  }
}
"""


@dataclass(frozen=True, slots=True, repr=False)
class TikTokPublicProfile:
    """Public presentation fields read from one already-bound profile page."""

    handle: str
    display_name: str
    avatar_png: bytes = field(repr=False)


class _UniversalDataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._inside_target = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() != "script":
            return
        attributes = {str(key).lower(): str(value or "") for key, value in attrs}
        self._inside_target = (
            attributes.get("id") == "__UNIVERSAL_DATA_FOR_REHYDRATION__"
        )

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script":
            self._inside_target = False

    def handle_data(self, data: str) -> None:
        if self._inside_target:
            self.parts.append(data)


def _profile_unavailable() -> TikTokIdentityError:
    return TikTokIdentityError(
        "tiktok_profile_unavailable",
        "TikTok 登录已确认，但昵称或头像暂未读取完成",
    )


def _parse_profile_payload(payload: object, *, expected_handle: str) -> tuple[str, str]:
    if not isinstance(payload, Mapping) or payload.get("state") != "ok":
        raise _profile_unavailable()
    handle = normalize_tiktok_handle(payload.get("uniqueId"))
    if not handle:
        raise _profile_unavailable()
    if handle != expected_handle:
        raise TikTokIdentityError(
            "tiktok_account_identity_mismatch",
            "TikTok 个人主页与已确认账号不一致",
        )
    display_name = " ".join(str(payload.get("nickname") or "").split())
    if not display_name or len(display_name) > 100:
        raise _profile_unavailable()
    return handle, display_name


def _response_header(response, name: str) -> str:
    try:
        value = response.headers.get(name) or response.headers.get(name.lower())
    except Exception:
        return ""
    return str(value or "").strip()


def _response_status(response) -> int:
    try:
        status = response.status_code
    except Exception:
        raise _profile_unavailable() from None
    if type(status) is not int:
        raise _profile_unavailable()
    return status


def _bounded_response_bytes(response, *, maximum: int) -> bytes:
    declared = _response_header(response, "Content-Length")
    if declared:
        try:
            size = int(declared)
        except ValueError:
            raise _profile_unavailable() from None
        if size < 0 or size > maximum:
            raise _profile_unavailable()
    collected = bytearray()
    try:
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not isinstance(chunk, bytes):
                raise _profile_unavailable()
            if len(collected) + len(chunk) > maximum:
                raise _profile_unavailable()
            collected.extend(chunk)
    except TikTokIdentityError:
        raise
    except Exception:
        raise _profile_unavailable() from None
    if not collected:
        raise _profile_unavailable()
    return bytes(collected)


def _validated_avatar_url(raw_url: object) -> str:
    if not isinstance(raw_url, str) or not raw_url:
        raise _profile_unavailable()
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except (TypeError, ValueError):
        raise _profile_unavailable() from None
    host = (parsed.hostname or "").lower().rstrip(".")
    trusted_host = any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in _APPROVED_AVATAR_HOST_SUFFIXES
    )
    if (
        parsed.scheme.lower() != "https"
        or not trusted_host
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise _profile_unavailable()
    return raw_url


def _profile_payload_from_html(raw_html: bytes) -> Mapping[str, Any]:
    try:
        parser = _UniversalDataParser()
        parser.feed(raw_html.decode("utf-8"))
        document = json.loads("".join(parser.parts))
        user = document["__DEFAULT_SCOPE__"]["webapp.user-detail"]["userInfo"]["user"]
    except (KeyError, TypeError, UnicodeDecodeError, ValueError):
        raise _profile_unavailable() from None
    if not isinstance(user, Mapping):
        raise _profile_unavailable()
    return {
        "state": "ok",
        "uniqueId": user.get("uniqueId"),
        "nickname": user.get("nickname"),
        "avatarThumb": user.get("avatarThumb"),
    }


def _download_public_avatar(url: str, *, transport, headers: Mapping[str, str]) -> bytes:
    current_url = _validated_avatar_url(url)
    for redirect_count in range(MAX_AVATAR_REDIRECTS + 1):
        response = None
        try:
            response = transport.get(
                current_url,
                timeout=PUBLIC_PROFILE_TIMEOUT_SECONDS,
                stream=True,
                allow_redirects=False,
                headers=dict(headers),
            )
            status = _response_status(response)
            if status in _REDIRECT_STATUS_CODES:
                if redirect_count >= MAX_AVATAR_REDIRECTS:
                    raise _profile_unavailable()
                location = _response_header(response, "Location")
                if not location:
                    raise _profile_unavailable()
                current_url = _validated_avatar_url(urljoin(current_url, location))
                continue
            if status != 200 or not _response_header(response, "Content-Type").lower().startswith("image/"):
                raise _profile_unavailable()
            return _normalized_png(
                _bounded_response_bytes(response, maximum=MAX_AVATAR_BYTES)
            )
        except TikTokIdentityError:
            raise
        except Exception:
            raise _profile_unavailable() from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
    raise _profile_unavailable()


def fetch_tiktok_public_profile(
    identity: TikTokIdentity,
    *,
    transport=None,
) -> TikTokPublicProfile:
    """Fetch one public profile without cookies when browser DOM is unavailable."""

    expected_handle = normalize_tiktok_handle(getattr(identity, "handle", ""))
    if not expected_handle:
        raise TikTokIdentityError(
            "tiktok_account_invalid", "TikTok 页面没有返回稳定账号标识"
        )
    session = transport or requests.Session()
    headers = {"User-Agent": _PUBLIC_PROFILE_USER_AGENT}
    response = None
    try:
        response = session.get(
            f"https://www.tiktok.com/@{expected_handle}",
            timeout=PUBLIC_PROFILE_TIMEOUT_SECONDS,
            stream=True,
            allow_redirects=False,
            headers=headers,
        )
        if _response_status(response) != 200:
            raise _profile_unavailable()
        content_type = _response_header(response, "Content-Type").lower()
        if content_type and "html" not in content_type:
            raise _profile_unavailable()
        payload = _profile_payload_from_html(
            _bounded_response_bytes(response, maximum=MAX_PROFILE_HTML_BYTES)
        )
    except TikTokIdentityError:
        raise
    except Exception:
        raise _profile_unavailable() from None
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass

    handle, display_name = _parse_profile_payload(
        payload,
        expected_handle=expected_handle,
    )
    avatar_url = _validated_avatar_url(payload.get("avatarThumb"))
    avatar_png = _download_public_avatar(
        avatar_url,
        transport=session,
        headers=headers,
    )
    return TikTokPublicProfile(
        handle=handle,
        display_name=display_name,
        avatar_png=avatar_png,
    )


async def _avatar_bytes(page) -> bytes:
    try:
        locator = page.locator(_PROFILE_AVATAR_SELECTOR).first
        if int(await locator.count()) != 1:
            raise _profile_unavailable()
        if not bool(await locator.is_visible(timeout=1_200)):
            raise _profile_unavailable()
        image = await locator.screenshot(type="png", timeout=10_000)
    except TikTokIdentityError:
        raise
    except Exception:
        raise _profile_unavailable() from None
    if not isinstance(image, bytes) or not image or len(image) > MAX_AVATAR_BYTES:
        raise _profile_unavailable()
    return image


async def read_tiktok_public_profile(
    page,
    identity: TikTokIdentity,
    *,
    poll_seconds: float = TIKTOK_PROFILE_POLL_SECONDS,
    max_attempts: int = TIKTOK_PROFILE_MAX_ATTEMPTS,
) -> TikTokPublicProfile:
    """Read nickname and avatar only from the exact verified profile URL."""

    expected_handle = normalize_tiktok_handle(getattr(identity, "handle", ""))
    if not expected_handle:
        raise TikTokIdentityError(
            "tiktok_account_invalid", "TikTok 页面没有返回稳定账号标识"
        )
    profile_url = f"https://www.tiktok.com/@{expected_handle}"
    try:
        await page.goto(
            profile_url,
            wait_until="domcontentloaded",
            timeout=TIKTOK_PROFILE_NAVIGATION_TIMEOUT_MS,
        )
    except Exception:
        raise _profile_unavailable() from None
    landed_handle = normalize_tiktok_handle(getattr(page, "url", ""))
    if landed_handle != expected_handle:
        if landed_handle:
            raise TikTokIdentityError(
                "tiktok_account_identity_mismatch",
                "TikTok 个人主页与已确认账号不一致",
            )
        raise _profile_unavailable()

    attempts = max(2, int(max_attempts))
    interval = max(0.0, float(poll_seconds))
    previous: tuple[str, str] | None = None
    for attempt in range(attempts):
        try:
            payload = await page.evaluate(_PROFILE_PUBLIC_DATA_EVALUATOR)
            current = _parse_profile_payload(
                payload,
                expected_handle=expected_handle,
            )
        except TikTokIdentityError as exc:
            if exc.error_code == "tiktok_account_identity_mismatch":
                raise
            current = None
        except Exception:
            current = None
        if current is not None:
            if previous == current:
                try:
                    avatar = await _avatar_bytes(page)
                except TikTokIdentityError as exc:
                    if exc.error_code != "tiktok_profile_unavailable":
                        raise
                else:
                    return TikTokPublicProfile(
                        handle=current[0],
                        display_name=current[1],
                        avatar_png=avatar,
                    )
            previous = current
        else:
            previous = None
        if attempt + 1 < attempts:
            await asyncio.sleep(interval)
    raise _profile_unavailable()


def _normalized_png(raw_image: bytes) -> bytes:
    if not isinstance(raw_image, bytes) or not raw_image:
        raise _profile_unavailable()
    if len(raw_image) > MAX_AVATAR_BYTES:
        raise _profile_unavailable()
    try:
        with Image.open(BytesIO(raw_image)) as decoded:
            width, height = decoded.size
            if width <= 0 or height <= 0 or width * height > MAX_AVATAR_PIXELS:
                raise _profile_unavailable()
            decoded.load()
            normalized = decoded.convert("RGBA")
        output = BytesIO()
        normalized.save(output, format="PNG", optimize=True)
        return output.getvalue()
    except TikTokIdentityError:
        raise
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError):
        raise _profile_unavailable() from None


def persist_tiktok_public_profile(
    account_id: int,
    profile: TikTokPublicProfile,
    *,
    avatar_dir: Path = AVATAR_DIR,
) -> str:
    """Persist public profile fields only when the stable handle still matches."""

    account_id = int(account_id)
    handle = normalize_tiktok_handle(getattr(profile, "handle", ""))
    display_name = " ".join(str(getattr(profile, "display_name", "") or "").split())
    if account_id <= 0 or not handle or not display_name or len(display_name) > 100:
        raise _profile_unavailable()
    png = _normalized_png(getattr(profile, "avatar_png", b""))

    with connect() as conn:
        row = conn.execute(
            "SELECT id, type, accountReference FROM user_info WHERE id = ?",
            (account_id,),
        ).fetchone()
        if not row or int(row["type"] or 0) != 6:
            raise TikTokIdentityError(
                "tiktok_account_invalid", "TikTok 账号记录不存在或类型不正确"
            )
        saved_handle = normalize_tiktok_handle(row["accountReference"])
        if not saved_handle or saved_handle != handle:
            raise TikTokIdentityError(
                "tiktok_account_identity_mismatch",
                "TikTok 个人主页与已确认账号不一致",
            )

        directory = Path(avatar_dir)
        file_name = f"oneclick_account_{account_id}.png"
        destination = directory / file_name
        temporary: Path | None = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            if directory.is_symlink() or destination.is_symlink():
                raise OSError("unsafe avatar path")
            with tempfile.NamedTemporaryFile(
                "wb",
                dir=directory,
                prefix=f".{file_name}.",
                suffix=".tmp",
                delete=False,
            ) as output:
                temporary = Path(output.name)
                output.write(png)
                output.flush()
                os.fsync(output.fileno())
            if os.name == "posix":
                temporary.chmod(0o600)

            updated = conn.execute(
                """
                UPDATE user_info
                SET userName = ?, avatarPath = ?, avatarUpdatedAt = ?
                WHERE id = ? AND type = 6 AND accountReference = ?
                """,
                (
                    display_name,
                    file_name,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    account_id,
                    row["accountReference"],
                ),
            )
            if int(updated.rowcount or 0) != 1:
                raise TikTokIdentityError(
                    "tiktok_account_invalid", "TikTok 账号记录在资料保存期间已变更"
                )

            # The database CAS must be durably committed before the fixed avatar
            # filename is replaced.  A rejected update or failed commit therefore
            # leaves the existing account avatar intact.
            conn.commit()
            os.replace(temporary, destination)
            temporary = None
            if os.name == "posix":
                destination.chmod(0o600)
        except OSError:
            raise _profile_unavailable() from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
    return file_name


__all__ = [
    "TikTokPublicProfile",
    "fetch_tiktok_public_profile",
    "persist_tiktok_public_profile",
    "read_tiktok_public_profile",
]
