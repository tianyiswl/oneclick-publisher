# -*- coding: utf-8 -*-
"""YouTube OAuth 客户配置与令牌的本机私密存储。"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app_core.paths import PRIVATE_DATA_DIR, ROOT_DIR


YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
YOUTUBE_REQUIRED_SCOPES = (
    YOUTUBE_UPLOAD_SCOPE,
    YOUTUBE_READONLY_SCOPE,
)
YOUTUBE_DATA_DIR = PRIVATE_DATA_DIR / "overseas" / "youtube"
DEFAULT_CLIENT_CONFIG_PATH = YOUTUBE_DATA_DIR / "oauth-client.json"
DEFAULT_TOKEN_PATH = YOUTUBE_DATA_DIR / "oauth-token.json"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc).replace(microsecond=0)
    return normalized.isoformat().replace("+00:00", "Z")


def _parse_time(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    """原子写入私密 JSON，并在 POSIX 上限制为当前用户可读写。"""

    _private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temporary_path.chmod(0o600)
        except OSError:
            pass
        temporary_path.replace(path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"YouTube 配置文件不存在：{path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"YouTube 配置文件无法读取：{path.name}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"YouTube 配置文件格式错误：{path.name}")
    return payload


def _inside_repository(path: Path) -> bool:
    try:
        path.resolve().relative_to(ROOT_DIR.resolve())
        return True
    except ValueError:
        return False


@dataclass(frozen=True, repr=False)
class OAuthClientConfig:
    client_id: str
    client_secret: str = ""

    @classmethod
    def from_google_json(cls, payload: dict[str, Any]) -> "OAuthClientConfig":
        installed = payload.get("installed")
        if not isinstance(installed, dict):
            raise ValueError("只支持 Google Cloud 创建的桌面应用 OAuth 客户端")
        client_id = str(installed.get("client_id") or "").strip()
        client_secret = str(installed.get("client_secret") or "").strip()
        if not client_id or not client_id.endswith(".apps.googleusercontent.com"):
            raise ValueError("YouTube OAuth 客户端 ID 格式错误")
        return cls(client_id=client_id, client_secret=client_secret)

    def as_google_json(self) -> dict[str, Any]:
        installed: dict[str, Any] = {"client_id": self.client_id}
        if self.client_secret:
            installed["client_secret"] = self.client_secret
        return {"installed": installed}


@dataclass(frozen=True, repr=False)
class TokenRecord:
    access_token: str
    refresh_token: str
    expires_at: datetime
    scopes: tuple[str, ...]
    token_type: str = "Bearer"
    refresh_token_expires_at: datetime | None = None
    channel_id: str = ""
    channel_title: str = ""

    def is_access_token_valid(
        self,
        *,
        now: datetime | None = None,
        skew_seconds: int = 120,
    ) -> bool:
        current = (now or _utc_now()).astimezone(timezone.utc)
        return bool(
            self.access_token
            and current + timedelta(seconds=max(skew_seconds, 0)) < self.expires_at
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "accessToken": self.access_token,
            "refreshToken": self.refresh_token,
            "expiresAt": _iso_utc(self.expires_at),
            "refreshTokenExpiresAt": (
                _iso_utc(self.refresh_token_expires_at)
                if self.refresh_token_expires_at
                else ""
            ),
            "scopes": list(self.scopes),
            "tokenType": self.token_type,
            "channelId": self.channel_id,
            "channelTitle": self.channel_title,
        }


class YouTubeCredentialStore:
    """只在系统用户数据目录保存 OAuth 配置和令牌。"""

    def __init__(
        self,
        *,
        client_config_path: Path = DEFAULT_CLIENT_CONFIG_PATH,
        token_path: Path = DEFAULT_TOKEN_PATH,
    ) -> None:
        self.client_config_path = Path(client_config_path)
        self.token_path = Path(token_path)
        if _inside_repository(self.client_config_path) or _inside_repository(
            self.token_path
        ):
            raise ValueError("YouTube OAuth 配置和令牌不得保存在代码仓库中")

    def configure_client(self, source: Path) -> OAuthClientConfig:
        source_path = Path(source)
        if _inside_repository(source_path):
            raise ValueError("YouTube OAuth 客户配置源文件不得放在代码仓库中")
        config = OAuthClientConfig.from_google_json(_read_json(source_path))
        _write_private_json(self.client_config_path, config.as_google_json())
        return config

    def load_client(self) -> OAuthClientConfig:
        return OAuthClientConfig.from_google_json(_read_json(self.client_config_path))

    def load_token(self) -> TokenRecord:
        payload = _read_json(self.token_path)
        if int(payload.get("schemaVersion") or 0) != 1:
            raise ValueError("YouTube OAuth 令牌版本不受支持")
        access_token = str(payload.get("accessToken") or "").strip()
        refresh_token = str(payload.get("refreshToken") or "").strip()
        expires_at = _parse_time(payload.get("expiresAt"))
        scopes_raw = payload.get("scopes")
        if not isinstance(scopes_raw, list):
            scopes_raw = []
        scopes = tuple(
            sorted(
                {
                    str(scope).strip()
                    for scope in scopes_raw
                    if isinstance(scope, str) and scope.strip()
                }
            )
        )
        if not access_token or not refresh_token or expires_at is None:
            raise ValueError("YouTube OAuth 令牌内容不完整，需要重新授权")
        missing_scopes = set(YOUTUBE_REQUIRED_SCOPES).difference(scopes)
        if missing_scopes:
            raise ValueError("YouTube OAuth 令牌缺少上传或频道只读权限")
        return TokenRecord(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            scopes=scopes,
            token_type=str(payload.get("tokenType") or "Bearer"),
            refresh_token_expires_at=_parse_time(
                payload.get("refreshTokenExpiresAt")
            ),
            channel_id=str(payload.get("channelId") or "").strip(),
            channel_title=str(payload.get("channelTitle") or "").strip(),
        )

    def save_token_response(
        self,
        response: dict[str, Any],
        *,
        now: datetime | None = None,
        existing: TokenRecord | None = None,
    ) -> TokenRecord:
        current = (now or _utc_now()).astimezone(timezone.utc)
        access_token = str(response.get("access_token") or "").strip()
        refresh_token = str(response.get("refresh_token") or "").strip()
        if not refresh_token and existing is not None:
            refresh_token = existing.refresh_token
        try:
            expires_in = max(1, int(response.get("expires_in") or 0))
        except (TypeError, ValueError):
            expires_in = 0
        if not access_token or not refresh_token or not expires_in:
            raise ValueError("YouTube OAuth 服务返回的令牌不完整")

        scope_value = response.get("scope")
        if isinstance(scope_value, str):
            scopes = tuple(sorted(set(scope_value.split())))
        elif existing is not None:
            scopes = existing.scopes
        else:
            scopes = ()
        missing_scopes = set(YOUTUBE_REQUIRED_SCOPES).difference(scopes)
        if missing_scopes:
            raise ValueError("用户未完整授予 YouTube 上传与频道只读权限")

        refresh_expiry = existing.refresh_token_expires_at if existing else None
        try:
            refresh_expires_in = int(response.get("refresh_token_expires_in") or 0)
        except (TypeError, ValueError):
            refresh_expires_in = 0
        if refresh_expires_in > 0:
            refresh_expiry = current + timedelta(seconds=refresh_expires_in)

        record = TokenRecord(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=current + timedelta(seconds=expires_in),
            scopes=scopes,
            token_type=str(response.get("token_type") or "Bearer"),
            refresh_token_expires_at=refresh_expiry,
            channel_id=existing.channel_id if existing else "",
            channel_title=existing.channel_title if existing else "",
        )
        _write_private_json(self.token_path, record.as_json())
        return record

    def save_channel(self, channel_id: str, channel_title: str) -> TokenRecord:
        existing = self.load_token()
        updated = TokenRecord(
            access_token=existing.access_token,
            refresh_token=existing.refresh_token,
            expires_at=existing.expires_at,
            scopes=existing.scopes,
            token_type=existing.token_type,
            refresh_token_expires_at=existing.refresh_token_expires_at,
            channel_id=str(channel_id or "").strip(),
            channel_title=str(channel_title or "").strip(),
        )
        _write_private_json(self.token_path, updated.as_json())
        return updated

    def status(self, *, now: datetime | None = None) -> dict[str, Any]:
        client_configured = self.client_config_path.is_file()
        token_configured = self.token_path.is_file()
        result: dict[str, Any] = {
            "clientConfigured": client_configured,
            "tokenConfigured": token_configured,
            "accessTokenValid": False,
            "scopeGranted": False,
            "channelId": "",
            "channelTitle": "",
            "expiresAt": "",
            "clientConfigPath": str(self.client_config_path),
            "tokenPath": str(self.token_path),
        }
        if not token_configured:
            return result
        try:
            token = self.load_token()
        except (FileNotFoundError, ValueError):
            return result
        result.update(
            {
                "accessTokenValid": token.is_access_token_valid(now=now),
                "scopeGranted": set(YOUTUBE_REQUIRED_SCOPES).issubset(token.scopes),
                "channelId": token.channel_id,
                "channelTitle": token.channel_title,
                "expiresAt": _iso_utc(token.expires_at),
            }
        )
        return result
