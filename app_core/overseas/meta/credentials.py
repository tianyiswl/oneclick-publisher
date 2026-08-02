# -*- coding: utf-8 -*-
"""Meta OAuth Broker 配置、用户令牌和 Page 资产的本机私密存储。"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app_core.paths import PRIVATE_DATA_DIR, ROOT_DIR


META_PAGE_LIST_SCOPE = "pages_show_list"
META_PAGE_READ_SCOPE = "pages_read_engagement"
META_PAGE_PUBLISH_SCOPE = "pages_manage_posts"
META_INSTAGRAM_BASIC_SCOPE = "instagram_basic"
META_INSTAGRAM_PUBLISH_SCOPE = "instagram_content_publish"
META_REQUIRED_SCOPES = (
    META_PAGE_LIST_SCOPE,
    META_PAGE_READ_SCOPE,
    META_PAGE_PUBLISH_SCOPE,
    META_INSTAGRAM_BASIC_SCOPE,
    META_INSTAGRAM_PUBLISH_SCOPE,
)
META_DATA_DIR = PRIVATE_DATA_DIR / "overseas" / "meta"
DEFAULT_BROKER_CONFIG_PATH = META_DATA_DIR / "oauth-broker.json"
DEFAULT_TOKEN_PATH = META_DATA_DIR / "oauth-token.json"
DEFAULT_GRAPH_VERSION = "v25.0"


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


def _normalize_tasks(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    return tuple(sorted({str(item).strip() for item in value if str(item).strip()}))


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
        raise FileNotFoundError(f"Meta 配置文件不存在：{path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Meta 配置文件无法读取：{path.name}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Meta 配置文件格式错误：{path.name}")
    return payload


def _inside_repository(path: Path) -> bool:
    try:
        path.resolve().relative_to(ROOT_DIR.resolve())
        return True
    except ValueError:
        return False


def _validate_broker_url(value: str) -> str:
    normalized = str(value or "").strip().rstrip("/")
    parsed = urlparse(normalized)
    allow_local_http = (
        os.getenv("ONECLICK_META_ALLOW_HTTP_BROKER", "").strip() == "1"
        or os.getenv("FASHETAI_META_ALLOW_HTTP_BROKER", "").strip() == "1"
    )
    local_host = str(parsed.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not (
        allow_local_http and local_host and parsed.scheme == "http"
    ):
        raise ValueError("Meta OAuth Broker 必须使用 HTTPS")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Meta OAuth Broker URL 格式错误")
    if parsed.query or parsed.fragment:
        raise ValueError("Meta OAuth Broker URL 不能包含查询参数或片段")
    return normalized


def _validate_graph_version(value: str) -> str:
    normalized = str(value or DEFAULT_GRAPH_VERSION).strip()
    if not re.fullmatch(r"v\d+\.\d+", normalized):
        raise ValueError("Meta Graph API 版本格式错误")
    return normalized


@dataclass(frozen=True, repr=False)
class MetaBrokerConfig:
    broker_url: str
    app_id: str
    graph_version: str = DEFAULT_GRAPH_VERSION

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "MetaBrokerConfig":
        lowered_keys = {str(key).lower() for key in payload}
        if {"app_secret", "appsecret", "client_secret", "clientsecret"}.intersection(
            lowered_keys
        ):
            raise ValueError("Meta App Secret 只能保存在 OAuth Broker 服务端")
        broker_url = _validate_broker_url(
            str(payload.get("brokerUrl") or payload.get("broker_url") or "")
        )
        app_id = str(payload.get("appId") or payload.get("app_id") or "").strip()
        if not app_id.isdigit():
            raise ValueError("Meta App ID 格式错误")
        return cls(
            broker_url=broker_url,
            app_id=app_id,
            graph_version=_validate_graph_version(
                str(payload.get("graphVersion") or payload.get("graph_version") or "")
            ),
        )

    def as_json(self) -> dict[str, str]:
        return {
            "brokerUrl": self.broker_url,
            "appId": self.app_id,
            "graphVersion": self.graph_version,
        }


@dataclass(frozen=True, repr=False)
class MetaPageAsset:
    page_id: str
    page_name: str
    page_access_token: str
    tasks: tuple[str, ...] = ()
    instagram_account_id: str = ""
    instagram_username: str = ""

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "MetaPageAsset":
        return cls(
            page_id=str(payload.get("pageId") or "").strip(),
            page_name=str(payload.get("pageName") or "").strip(),
            page_access_token=str(payload.get("pageAccessToken") or "").strip(),
            tasks=_normalize_tasks(payload.get("tasks")),
            instagram_account_id=str(
                payload.get("instagramAccountId") or ""
            ).strip(),
            instagram_username=str(payload.get("instagramUsername") or "").strip(),
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "pageId": self.page_id,
            "pageName": self.page_name,
            "pageAccessToken": self.page_access_token,
            "tasks": list(self.tasks),
            "instagramAccountId": self.instagram_account_id,
            "instagramUsername": self.instagram_username,
        }


@dataclass(frozen=True, repr=False)
class MetaTokenRecord:
    user_access_token: str
    expires_at: datetime
    scopes: tuple[str, ...]
    app_scoped_user_id: str
    app_id: str
    grant_id: str = ""
    assets: tuple[MetaPageAsset, ...] = ()
    token_type: str = "Bearer"

    def is_access_token_valid(
        self,
        *,
        now: datetime | None = None,
        skew_seconds: int = 300,
    ) -> bool:
        current = (now or _utc_now()).astimezone(timezone.utc)
        return bool(
            self.user_access_token
            and current + timedelta(seconds=max(0, skew_seconds)) < self.expires_at
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "userAccessToken": self.user_access_token,
            "expiresAt": _iso_utc(self.expires_at),
            "scopes": list(self.scopes),
            "appScopedUserId": self.app_scoped_user_id,
            "appId": self.app_id,
            "grantId": self.grant_id,
            "tokenType": self.token_type,
            "assets": [asset.as_json() for asset in self.assets],
        }


class MetaCredentialStore:
    """只在系统用户数据目录保存 Broker 配置、Meta 令牌和 Page 资产。"""

    def __init__(
        self,
        *,
        broker_config_path: Path = DEFAULT_BROKER_CONFIG_PATH,
        token_path: Path = DEFAULT_TOKEN_PATH,
    ) -> None:
        self.broker_config_path = Path(broker_config_path)
        self.token_path = Path(token_path)
        if _inside_repository(self.broker_config_path) or _inside_repository(
            self.token_path
        ):
            raise ValueError("Meta Broker 配置和令牌不得保存在代码仓库中")

    def configure_broker(self, source: Path) -> MetaBrokerConfig:
        source_path = Path(source)
        if _inside_repository(source_path):
            raise ValueError("Meta Broker 配置源文件不得放在代码仓库中")
        config = MetaBrokerConfig.from_json(_read_json(source_path))
        _write_private_json(self.broker_config_path, config.as_json())
        return config

    def load_broker(self) -> MetaBrokerConfig:
        return MetaBrokerConfig.from_json(_read_json(self.broker_config_path))

    def load_token(self) -> MetaTokenRecord:
        payload = _read_json(self.token_path)
        if int(payload.get("schemaVersion") or 0) != 1:
            raise ValueError("Meta OAuth 令牌版本不受支持")
        access_token = str(payload.get("userAccessToken") or "").strip()
        expires_at = _parse_time(payload.get("expiresAt"))
        scopes = _normalize_scopes(payload.get("scopes"))
        user_id = str(payload.get("appScopedUserId") or "").strip()
        app_id = str(payload.get("appId") or "").strip()
        assets_raw = payload.get("assets")
        assets = tuple(
            MetaPageAsset.from_json(item)
            for item in (assets_raw if isinstance(assets_raw, list) else [])
            if isinstance(item, dict)
        )
        if not access_token or expires_at is None or not user_id or not app_id:
            raise ValueError("Meta OAuth 令牌内容不完整，需要重新授权")
        if not set(META_REQUIRED_SCOPES).issubset(scopes):
            raise ValueError("Meta OAuth 令牌缺少 Page 或 Instagram 发布权限")
        if any(not asset.page_id or not asset.page_access_token for asset in assets):
            raise ValueError("Meta Page 资产内容不完整，需要重新读取")
        return MetaTokenRecord(
            user_access_token=access_token,
            expires_at=expires_at,
            scopes=scopes,
            app_scoped_user_id=user_id,
            app_id=app_id,
            grant_id=str(payload.get("grantId") or "").strip(),
            assets=assets,
            token_type=str(payload.get("tokenType") or "Bearer"),
        )

    def save_authorization_response(
        self,
        response: dict[str, Any],
        *,
        now: datetime | None = None,
        existing: MetaTokenRecord | None = None,
    ) -> MetaTokenRecord:
        current = (now or _utc_now()).astimezone(timezone.utc)
        access_token = str(
            response.get("accessToken") or response.get("access_token") or ""
        ).strip()
        user_id = str(
            response.get("appScopedUserId") or response.get("user_id") or ""
        ).strip()
        app_id = str(response.get("appId") or response.get("app_id") or "").strip()
        try:
            expires_in = int(response.get("expiresIn") or response.get("expires_in") or 0)
        except (TypeError, ValueError):
            expires_in = 0
        scopes = _normalize_scopes(response.get("scopes") or response.get("scope"))
        if not scopes and existing is not None:
            scopes = existing.scopes
        if not access_token or not user_id or not app_id or expires_in <= 0:
            raise ValueError("Meta OAuth Broker 返回的授权数据不完整")
        config = self.load_broker()
        if app_id != config.app_id:
            raise PermissionError("Meta OAuth Broker 返回的 App ID 与客户端配置不一致")
        if not set(META_REQUIRED_SCOPES).issubset(scopes):
            raise ValueError("用户未完整授予 Meta Page 与 Instagram 发布权限")
        record = MetaTokenRecord(
            user_access_token=access_token,
            expires_at=current + timedelta(seconds=expires_in),
            scopes=scopes,
            app_scoped_user_id=user_id,
            app_id=app_id,
            grant_id=str(response.get("grantId") or "").strip()
            or (existing.grant_id if existing else ""),
            assets=existing.assets if existing else (),
            token_type=str(response.get("tokenType") or "Bearer"),
        )
        _write_private_json(self.token_path, record.as_json())
        return record

    def save_assets(self, assets: tuple[MetaPageAsset, ...]) -> MetaTokenRecord:
        existing = self.load_token()
        normalized = tuple(assets)
        if any(not asset.page_id or not asset.page_access_token for asset in normalized):
            raise ValueError("Meta Page 资产缺少 ID 或 Page Access Token")
        updated = MetaTokenRecord(
            user_access_token=existing.user_access_token,
            expires_at=existing.expires_at,
            scopes=existing.scopes,
            app_scoped_user_id=existing.app_scoped_user_id,
            app_id=existing.app_id,
            grant_id=existing.grant_id,
            assets=normalized,
            token_type=existing.token_type,
        )
        _write_private_json(self.token_path, updated.as_json())
        return updated

    def page_asset(self, account_reference: str, *, instagram: bool) -> MetaPageAsset:
        target = str(account_reference or "").strip()
        if not target:
            raise ValueError("Meta 操作需要选择目标 Page 或 Instagram 专业账号")
        for asset in self.load_token().assets:
            candidate = asset.instagram_account_id if instagram else asset.page_id
            if candidate == target:
                return asset
        raise PermissionError("目标 Meta 资产不属于当前授权账号")

    def status(self, *, now: datetime | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "brokerConfigured": self.broker_config_path.is_file(),
            "tokenConfigured": self.token_path.is_file(),
            "accessTokenValid": False,
            "scopeGranted": False,
            "pageCount": 0,
            "instagramAccountCount": 0,
            "expiresAt": "",
            "brokerConfigPath": str(self.broker_config_path),
            "tokenPath": str(self.token_path),
        }
        if not result["tokenConfigured"]:
            return result
        try:
            token = self.load_token()
        except (FileNotFoundError, ValueError, PermissionError):
            return result
        result.update(
            {
                "accessTokenValid": token.is_access_token_valid(now=now),
                "scopeGranted": set(META_REQUIRED_SCOPES).issubset(token.scopes),
                "pageCount": len(token.assets),
                "instagramAccountCount": sum(
                    1 for asset in token.assets if asset.instagram_account_id
                ),
                "expiresAt": _iso_utc(token.expires_at),
            }
        )
        return result
