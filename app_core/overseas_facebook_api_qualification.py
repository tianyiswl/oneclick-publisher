"""Read-only Facebook Page Graph API qualification probe.

The probe deliberately does not request a Page access token and never calls a
write endpoint.  It proves only whether one user token currently exposes the
required Page scopes, the exact saved Page, and its content-creation task.  It
does not prove which Meta App issued the token, App Review, or production API
readiness.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import requests


DEFAULT_GRAPH_VERSION = "v25.0"
FACEBOOK_PAGE_API_KEYRING_SERVICE = (
    "com.hellomobai.yijianfa.facebook.api.qualification"
)
REQUIRED_FACEBOOK_PAGE_SCOPES = (
    "pages_manage_posts",
    "pages_read_engagement",
    "pages_show_list",
)
FACEBOOK_CREATE_CONTENT_TASK = "PROFILE_PLUS_CREATE_CONTENT"

_GRAPH_VERSION = re.compile(r"^v[1-9][0-9]*\.0$")
_TASK = re.compile(r"^[A-Z][A-Z0-9_]{0,99}$")
_MAX_ACCOUNT_PAGES = 20


class FacebookPageApiQualificationError(RuntimeError):
    """A stable, secret-free qualification failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code or "facebook_api_qualification_failed")
        super().__init__(str(message or "Facebook Page API 资格预检失败。"))


@dataclass(frozen=True, slots=True)
class FacebookPageApiQualificationResult:
    status: str
    target_page_id: str
    page_permission_check_passed: bool = False
    page_name: str = ""
    granted_scopes: tuple[str, ...] = ()
    missing_scopes: tuple[str, ...] = ()
    page_tasks: tuple[str, ...] = ()
    create_content_task_granted: bool = False

    def to_public_dict(self) -> dict[str, object]:
        """Project only the allowlisted, credential-free result fields."""

        return {
            "status": self.status,
            # Backward-safe: scope/task evidence alone must never be treated as
            # a completed API qualification or a publishing authorization.
            "qualified": False,
            "pagePermissionCheckPassed": self.page_permission_check_passed,
            "appAuthorizationVerified": False,
            "apiModeUsable": False,
            "targetPageId": self.target_page_id,
            "pageName": self.page_name,
            "grantedScopes": list(self.granted_scopes),
            "missingScopes": list(self.missing_scopes),
            "pageTasks": list(self.page_tasks),
            "createContentTaskGranted": self.create_content_task_granted,
            "writeAttempted": False,
        }


def _normalize_page_id(value: object) -> str:
    from .overseas_meta_page_identity import normalize_facebook_page_id

    try:
        return normalize_facebook_page_id(value)
    except Exception:
        raise FacebookPageApiQualificationError(
            "facebook_api_target_page_invalid",
            "Facebook Page API 资格预检缺少有效目标 Page ID。",
        ) from None


def _normalize_token(value: object) -> str:
    if not isinstance(value, str):
        raise FacebookPageApiQualificationError(
            "facebook_api_authorization_required",
            "Facebook Page API 资格预检需要 Meta 用户授权。",
        )
    normalized = value.strip()
    if not normalized or normalized != value or any(ch.isspace() for ch in value):
        raise FacebookPageApiQualificationError(
            "facebook_api_authorization_required",
            "Facebook Page API 资格预检需要 Meta 用户授权。",
        )
    return normalized


def _safe_page_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = " ".join(value.split())
    return normalized[:200]


def _safe_cursor(value: object) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        return None
    if not all(0x21 <= ord(character) <= 0x7E for character in value):
        return None
    return value


def _safe_tasks(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        sorted(
            {
                item
                for item in value
                if isinstance(item, str) and _TASK.fullmatch(item)
            }
        )
    )


class FacebookPageApiQualificationClient:
    """Use GET-only Graph calls to test one exact Page's API eligibility."""

    def __init__(
        self,
        *,
        session: Any | None = None,
        graph_version: str = DEFAULT_GRAPH_VERSION,
        timeout_seconds: float = 30.0,
    ) -> None:
        normalized_version = str(graph_version or "").strip()
        if not _GRAPH_VERSION.fullmatch(normalized_version):
            raise FacebookPageApiQualificationError(
                "facebook_api_graph_version_invalid",
                "Facebook Graph API 版本配置无效。",
            )
        self._session = session or requests.Session()
        self._graph_root = f"https://graph.facebook.com/{normalized_version}"
        self._timeout_seconds = max(5.0, min(float(timeout_seconds), 120.0))

    def __repr__(self) -> str:
        return "FacebookPageApiQualificationClient(<read-only>)"

    def _request_json(
        self,
        path: str,
        *,
        token: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        try:
            response = self._session.request(
                "GET",
                f"{self._graph_root}{path}",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {token}",
                },
                params=dict(params or {}),
                timeout=self._timeout_seconds,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise FacebookPageApiQualificationError(
                "facebook_api_unavailable",
                "Meta Graph API 当前无法连接。",
            ) from None
        except Exception:
            raise FacebookPageApiQualificationError(
                "facebook_api_unavailable",
                "Meta Graph API 当前无法连接。",
            ) from None

        try:
            payload = response.json()
        except Exception:
            payload = None
        status_code = int(getattr(response, "status_code", 0) or 0)
        error_code: int | None = None
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            raw_error_code = payload["error"].get("code")
            if type(raw_error_code) is int:
                error_code = raw_error_code
            elif isinstance(raw_error_code, str) and raw_error_code.isdigit():
                error_code = int(raw_error_code)
        if not 200 <= status_code < 300 or error_code is not None:
            if status_code == 401 or error_code == 190:
                raise FacebookPageApiQualificationError(
                    "facebook_api_token_rejected",
                    "Meta 用户授权已失效或不被接受。",
                )
            if status_code == 403:
                raise FacebookPageApiQualificationError(
                    "facebook_api_permission_denied",
                    "Meta 拒绝读取 Page API 资格。",
                )
            raise FacebookPageApiQualificationError(
                "facebook_api_request_failed",
                "Meta Graph API 资格读取失败。",
            )
        if not isinstance(payload, dict):
            raise FacebookPageApiQualificationError(
                "facebook_api_response_invalid",
                "Meta Graph API 返回了无法验证的资格结果。",
            )
        return payload

    def _granted_scopes(self, token: str) -> tuple[str, ...]:
        payload = self._request_json("/me/permissions", token=token)
        data = payload.get("data")
        if not isinstance(data, list):
            raise FacebookPageApiQualificationError(
                "facebook_api_response_invalid",
                "Meta 权限响应无法验证。",
            )
        granted: set[str] = set()
        for item in data:
            if not isinstance(item, dict):
                raise FacebookPageApiQualificationError(
                    "facebook_api_response_invalid",
                    "Meta 权限响应无法验证。",
                )
            permission = item.get("permission")
            status = item.get("status")
            if (
                isinstance(permission, str)
                and permission in REQUIRED_FACEBOOK_PAGE_SCOPES
                and status == "granted"
            ):
                granted.add(permission)
        return tuple(sorted(granted))

    def _find_page(
        self,
        *,
        token: str,
        target_page_id: str,
    ) -> tuple[str, tuple[str, ...]] | None:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _page_index in range(_MAX_ACCOUNT_PAGES):
            params: dict[str, object] = {
                "fields": "id,name,tasks",
                "limit": 100,
            }
            if cursor is not None:
                params["after"] = cursor
            payload = self._request_json(
                "/me/accounts",
                token=token,
                params=params,
            )
            data = payload.get("data")
            if not isinstance(data, list):
                raise FacebookPageApiQualificationError(
                    "facebook_api_response_invalid",
                    "Meta Page 列表响应无法验证。",
                )
            for item in data:
                if not isinstance(item, dict):
                    raise FacebookPageApiQualificationError(
                        "facebook_api_response_invalid",
                        "Meta Page 列表响应无法验证。",
                    )
                if str(item.get("id") or "").strip() == target_page_id:
                    return _safe_page_name(item.get("name")), _safe_tasks(
                        item.get("tasks")
                    )

            paging = payload.get("paging")
            cursors = paging.get("cursors") if isinstance(paging, dict) else None
            raw_next_cursor = (
                cursors.get("after") if isinstance(cursors, dict) else None
            )
            next_cursor = _safe_cursor(raw_next_cursor)
            if raw_next_cursor is not None and next_cursor is None:
                raise FacebookPageApiQualificationError(
                    "facebook_api_response_invalid",
                    "Meta Page 列表分页无法验证。",
                )
            if next_cursor is None:
                return None
            if next_cursor in seen_cursors:
                raise FacebookPageApiQualificationError(
                    "facebook_api_response_invalid",
                    "Meta Page 列表分页无法验证。",
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise FacebookPageApiQualificationError(
            "facebook_api_response_invalid",
            "Meta Page 列表超过安全读取上限。",
        )

    def qualify(
        self,
        *,
        user_access_token: str,
        target_page_id: str,
    ) -> FacebookPageApiQualificationResult:
        """Run the zero-write qualification contract for one saved Page."""

        token = _normalize_token(user_access_token)
        page_id = _normalize_page_id(target_page_id)
        granted_scopes = self._granted_scopes(token)
        missing_scopes = tuple(
            scope
            for scope in REQUIRED_FACEBOOK_PAGE_SCOPES
            if scope not in granted_scopes
        )
        if missing_scopes:
            return FacebookPageApiQualificationResult(
                status="permissions_missing",
                target_page_id=page_id,
                granted_scopes=granted_scopes,
                missing_scopes=missing_scopes,
            )

        page = self._find_page(token=token, target_page_id=page_id)
        if page is None:
            return FacebookPageApiQualificationResult(
                status="target_page_not_found",
                target_page_id=page_id,
                granted_scopes=granted_scopes,
            )
        page_name, tasks = page
        create_content_granted = FACEBOOK_CREATE_CONTENT_TASK in tasks
        return FacebookPageApiQualificationResult(
            status=(
                "page_scope_task_verified"
                if create_content_granted
                else "create_content_task_missing"
            ),
            target_page_id=page_id,
            page_permission_check_passed=create_content_granted,
            page_name=page_name,
            granted_scopes=granted_scopes,
            page_tasks=tasks,
            create_content_task_granted=create_content_granted,
        )


class _KeyringBackend(Protocol):
    def set_password(self, service: str, username: str, password: str) -> None: ...

    def get_password(self, service: str, username: str) -> str | None: ...

    def delete_password(self, service: str, username: str) -> None: ...


def _default_keyring_backend() -> _KeyringBackend:
    try:
        import keyring
    except Exception:
        raise FacebookPageApiQualificationError(
            "facebook_api_credential_unavailable",
            "系统凭据库不可用。",
        ) from None
    return keyring


class KeyringFacebookPageApiCredentialStore:
    """Store only user tokens in the operating-system credential store."""

    def __init__(
        self,
        *,
        backend: _KeyringBackend | None = None,
        service_name: str = FACEBOOK_PAGE_API_KEYRING_SERVICE,
    ) -> None:
        normalized_service = str(service_name or "").strip()
        if not normalized_service:
            raise FacebookPageApiQualificationError(
                "facebook_api_credential_unavailable",
                "系统凭据库配置无效。",
            )
        self._backend = backend
        self._service_name = normalized_service

    def __repr__(self) -> str:
        return "KeyringFacebookPageApiCredentialStore(<system credential store>)"

    def _keyring(self) -> _KeyringBackend:
        if self._backend is None:
            self._backend = _default_keyring_backend()
        return self._backend

    @staticmethod
    def _reference(page_id: object) -> str:
        return f"facebook-page-api:{_normalize_page_id(page_id)}"

    def load_user_access_token(self, page_id: object) -> str | None:
        try:
            token = self._keyring().get_password(
                self._service_name,
                self._reference(page_id),
            )
        except FacebookPageApiQualificationError:
            raise
        except Exception:
            raise FacebookPageApiQualificationError(
                "facebook_api_credential_unavailable",
                "系统凭据库不可用。",
            ) from None
        if token is None:
            return None
        return _normalize_token(token)

    def has_user_access_token(self, page_id: object) -> bool:
        return self.load_user_access_token(page_id) is not None

    def save_user_access_token(self, page_id: object, token: object) -> None:
        normalized_token = _normalize_token(token)
        try:
            self._keyring().set_password(
                self._service_name,
                self._reference(page_id),
                normalized_token,
            )
        except FacebookPageApiQualificationError:
            raise
        except Exception:
            raise FacebookPageApiQualificationError(
                "facebook_api_credential_unavailable",
                "系统凭据库不可用。",
            ) from None


def load_saved_facebook_page_account_read_only(
    account_id: object,
    *,
    database_path: str | Path | None = None,
) -> dict[str, object] | None:
    """Read one account row without schema migration or metadata updates."""

    try:
        normalized_account_id = int(account_id)
    except (TypeError, ValueError):
        normalized_account_id = 0
    if normalized_account_id <= 0:
        raise FacebookPageApiQualificationError(
            "facebook_api_saved_account_invalid",
            "Facebook Page 账号 ID 无效。",
        )
    if database_path is None:
        from .paths import DB_PATH

        path = Path(DB_PATH)
    else:
        path = Path(database_path)
    try:
        uri = f"{path.expanduser().resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT id, type, filePath, userName, status, profileName,
                       avatarPath, avatarUpdatedAt, remark, lastCheckedAt,
                       lastLoginAt, COALESCE(authMode, 'browser') AS authMode,
                       accountReference,
                       COALESCE(oauthScopeVersion, 1) AS oauthScopeVersion
                FROM user_info
                WHERE id = ?
                """,
                (normalized_account_id,),
            ).fetchone()
    except sqlite3.Error:
        raise FacebookPageApiQualificationError(
            "facebook_api_local_account_unavailable",
            "一键发账号数据库当前无法只读打开。",
        ) from None
    return dict(row) if row is not None else None


def _saved_facebook_page_id(account: object) -> str:
    from .account_service import validate_saved_facebook_page_account

    try:
        return _normalize_page_id(validate_saved_facebook_page_account(account))
    except FacebookPageApiQualificationError:
        raise
    except Exception:
        raise FacebookPageApiQualificationError(
            "facebook_api_saved_account_invalid",
            "一键发中保存的 Facebook Page 账号无法用于 API 资格预检。",
        ) from None


def qualify_saved_facebook_page_account(
    account: object,
    *,
    credential_store: Any | None = None,
    client: FacebookPageApiQualificationClient | None = None,
) -> dict[str, object]:
    """Qualify one exact saved Page without using its browser session."""

    page_id = _saved_facebook_page_id(account)
    store = credential_store or KeyringFacebookPageApiCredentialStore()
    token = store.load_user_access_token(page_id)
    if token is None:
        page_name = (
            _safe_page_name(account.get("userName"))
            if isinstance(account, dict)
            else ""
        )
        return FacebookPageApiQualificationResult(
            status="authorization_required",
            target_page_id=page_id,
            page_name=page_name,
            missing_scopes=REQUIRED_FACEBOOK_PAGE_SCOPES,
        ).to_public_dict()
    probe = client or FacebookPageApiQualificationClient()
    return probe.qualify(
        user_access_token=token,
        target_page_id=page_id,
    ).to_public_dict()


def check_supplied_user_access_token(
    account: object,
    user_access_token: object,
    *,
    client: FacebookPageApiQualificationClient | None = None,
) -> dict[str, object]:
    """Check a hidden-input token in memory without persisting it."""

    page_id = _saved_facebook_page_id(account)
    token = _normalize_token(user_access_token)
    probe = client or FacebookPageApiQualificationClient()
    result = probe.qualify(
        user_access_token=token,
        target_page_id=page_id,
    )
    public_result = result.to_public_dict()
    public_result["credentialStored"] = False
    return public_result
