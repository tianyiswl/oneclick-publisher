# -*- coding: utf-8 -*-
"""TikTok OAuth 客户配置与令牌的本机私密存储。"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app_core.paths import PRIVATE_DATA_DIR, ROOT_DIR


TIKTOK_BASIC_SCOPE = "user.info.basic"
TIKTOK_UPLOAD_SCOPE = "video.upload"
TIKTOK_REQUIRED_SCOPES = (TIKTOK_BASIC_SCOPE, TIKTOK_UPLOAD_SCOPE)
TIKTOK_DATA_DIR = PRIVATE_DATA_DIR / "overseas" / "tiktok"
DEFAULT_CLIENT_CONFIG_PATH = TIKTOK_DATA_DIR / "oauth-client.json"
DEFAULT_TOKEN_PATH = TIKTOK_DATA_DIR / "oauth-token.json"


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


def _normalize_scopes(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        raw = value.replace(",", " ").split()
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw = [str(item) for item in value]
    else:
        raw = []
    return tuple(sorted({scope.strip() for scope in raw if scope.strip()}))


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
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
        raise FileNotFoundError(f"TikTok 配置文件不存在：{path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"TikTok 配置文件无法读取：{path.name}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"TikTok 配置文件格式错误：{path.name}")
    return payload


def _inside_repository(path: Path) -> bool:
    try:
        path.resolve().relative_to(ROOT_DIR.resolve())
        return True
    except ValueError:
        return False


@dataclass(frozen=True, repr=False)
class OAuthClientConfig:
    client_key: str
    client_secret: str

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "OAuthClientConfig":
        nested = payload
        for key in ("desktop", "web", "installed"):
            candidate = payload.get(key)
            if isinstance(candidate, dict):
                nested = candidate
                break
        client_key = str(
            nested.get("client_key") or nested.get("clientKey") or ""
        ).strip()
        client_secret = str(
            nested.get("client_secret") or nested.get("clientSecret") or ""
        ).strip()
        if not client_key or not client_secret:
            raise ValueError("TikTok OAuth 客户配置必须包含 client_key 和 client_secret")
        return cls(client_key=client_key, client_secret=client_secret)

    def as_json(self) -> dict[str, str]:
        return {
            "client_key": self.client_key,
            "client_secret": self.client_secret,
        }


@dataclass(frozen=True, repr=False)
class TokenRecord:
    access_token: str
    refresh_token: str
    expires_at: datetime
    refresh_token_expires_at: datetime
    scopes: tuple[str, ...]
    open_id: str
    token_type: str = "Bearer"
    display_name: str = ""

    def is_access_token_valid(
        self,
        *,
        now: datetime | None = None,
        skew_seconds: int = 120,
    ) -> bool:
        current = (now or _utc_now()).astimezone(timezone.utc)
        return bool(
            self.access_token
            and current + timedelta(seconds=max(0, skew_seconds)) < self.expires_at
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "accessToken": self.access_token,
            "refreshToken": self.refresh_token,
            "expiresAt": _iso_utc(self.expires_at),
            "refreshTokenExpiresAt": _iso_utc(self.refresh_token_expires_at),
            "scopes": list(self.scopes),
            "openId": self.open_id,
            "tokenType": self.token_type,
            "displayName": self.display_name,
        }


class TikTokCredentialStore:
    """只在系统用户数据目录保存 TikTok OAuth 配置和令牌。"""

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
            raise ValueError("TikTok OAuth 配置和令牌不得保存在代码仓库中")

    def configure_client(self, source: Path) -> OAuthClientConfig:
        source_path = Path(source)
        if _inside_repository(source_path):
            raise ValueError("TikTok OAuth 客户配置源文件不得放在代码仓库中")
        config = OAuthClientConfig.from_json(_read_json(source_path))
        _write_private_json(self.client_config_path, config.as_json())
        return config

    def load_client(self) -> OAuthClientConfig:
        return OAuthClientConfig.from_json(_read_json(self.client_config_path))

    def load_token(self) -> TokenRecord:
        payload = _read_json(self.token_path)
        if int(payload.get("schemaVersion") or 0) != 1:
            raise ValueError("TikTok OAuth 令牌版本不受支持")
        access_token = str(payload.get("accessToken") or "").strip()
        refresh_token = str(payload.get("refreshToken") or "").strip()
        expires_at = _parse_time(payload.get("expiresAt"))
        refresh_expires_at = _parse_time(payload.get("refreshTokenExpiresAt"))
        scopes = _normalize_scopes(payload.get("scopes"))
        open_id = str(payload.get("openId") or "").strip()
        if (
            not access_token
            or not refresh_token
            or not open_id
            or expires_at is None
            or refresh_expires_at is None
        ):
            raise ValueError("TikTok OAuth 令牌内容不完整，需要重新授权")
        if not set(TIKTOK_REQUIRED_SCOPES).issubset(scopes):
            raise ValueError("TikTok OAuth 令牌缺少账号读取或视频上传权限")
        return TokenRecord(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            refresh_token_expires_at=refresh_expires_at,
            scopes=scopes,
            open_id=open_id,
            token_type=str(payload.get("tokenType") or "Bearer"),
            display_name=str(payload.get("displayName") or "").strip(),
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
        open_id = str(response.get("open_id") or "").strip()
        if existing is not None:
            refresh_token = refresh_token or existing.refresh_token
            open_id = open_id or existing.open_id
        try:
            expires_in = int(response.get("expires_in") or 0)
            refresh_expires_in = int(response.get("refresh_expires_in") or 0)
        except (TypeError, ValueError):
            expires_in = refresh_expires_in = 0
        if existing is not None and refresh_expires_in <= 0:
            refresh_expires_at = existing.refresh_token_expires_at
        else:
            refresh_expires_at = current + timedelta(seconds=max(0, refresh_expires_in))
        if not access_token or not refresh_token or not open_id or expires_in <= 0:
            raise ValueError("TikTok OAuth 服务返回的令牌不完整")
        if refresh_expires_at <= current:
            raise ValueError("TikTok OAuth 服务未返回有效的刷新令牌期限")

        scopes = _normalize_scopes(response.get("scope"))
        if not scopes and existing is not None:
            scopes = existing.scopes
        if not set(TIKTOK_REQUIRED_SCOPES).issubset(scopes):
            raise ValueError("用户未完整授予 TikTok 账号读取与视频上传权限")

        record = TokenRecord(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=current + timedelta(seconds=expires_in),
            refresh_token_expires_at=refresh_expires_at,
            scopes=scopes,
            open_id=open_id,
            token_type=str(response.get("token_type") or "Bearer"),
            display_name=existing.display_name if existing else "",
        )
        _write_private_json(self.token_path, record.as_json())
        return record

    def save_identity(self, open_id: str, display_name: str) -> TokenRecord:
        existing = self.load_token()
        normalized_open_id = str(open_id or "").strip()
        if not normalized_open_id or normalized_open_id != existing.open_id:
            raise PermissionError("TikTok 回读账号与授权令牌身份不一致")
        updated = TokenRecord(
            access_token=existing.access_token,
            refresh_token=existing.refresh_token,
            expires_at=existing.expires_at,
            refresh_token_expires_at=existing.refresh_token_expires_at,
            scopes=existing.scopes,
            open_id=existing.open_id,
            token_type=existing.token_type,
            display_name=str(display_name or "").strip(),
        )
        _write_private_json(self.token_path, updated.as_json())
        return updated

    def status(self, *, now: datetime | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "clientConfigured": self.client_config_path.is_file(),
            "tokenConfigured": self.token_path.is_file(),
            "accessTokenValid": False,
            "scopeGranted": False,
            "openId": "",
            "displayName": "",
            "expiresAt": "",
            "refreshTokenExpiresAt": "",
            "clientConfigPath": str(self.client_config_path),
            "tokenPath": str(self.token_path),
        }
        if not result["tokenConfigured"]:
            return result
        try:
            token = self.load_token()
        except (FileNotFoundError, ValueError):
            return result
        result.update(
            {
                "accessTokenValid": token.is_access_token_valid(now=now),
                "scopeGranted": set(TIKTOK_REQUIRED_SCOPES).issubset(token.scopes),
                "openId": token.open_id,
                "displayName": token.display_name,
                "expiresAt": _iso_utc(token.expires_at),
                "refreshTokenExpiresAt": _iso_utc(token.refresh_token_expires_at),
            }
        )
        return result
