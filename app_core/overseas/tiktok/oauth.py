# -*- coding: utf-8 -*-
"""TikTok Desktop Login Kit OAuth 2.0 + PKCE 授权流程。"""

from __future__ import annotations

import hashlib
import secrets
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from .credentials import (
    TIKTOK_REQUIRED_SCOPES,
    OAuthClientConfig,
    TikTokCredentialStore,
    TokenRecord,
)


AUTHORIZATION_ENDPOINT = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_ENDPOINT = "https://open.tiktokapis.com/v2/oauth/token/"
CALLBACK_PATH = "/callback/"
CALLBACK_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; "
        "script-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"
    ),
}


class TikTokOAuthError(RuntimeError):
    """不包含原始令牌、授权码或客户端密钥的 OAuth 错误。"""


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = hashlib.sha256(verifier.encode("ascii")).hexdigest()
    return verifier, challenge


def _safe_oauth_error(response: requests.Response, fallback: str) -> str:
    try:
        payload = response.json()
    except Exception:
        payload = {}
    if isinstance(payload, dict):
        description = str(payload.get("error_description") or "").strip()
        error = payload.get("error")
        if isinstance(error, dict):
            description = description or str(error.get("message") or "").strip()
            code = str(error.get("code") or "").strip()
        else:
            code = str(error or "").strip()
        if description:
            return description[:500]
        if code:
            return f"OAuth 错误：{code[:100]}"
    return fallback


def _callback_success_page() -> bytes:
    """返回不包含授权码并立即清理地址栏查询参数的成功页。"""

    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>一键发 TikTok 授权</title>"
        "<meta name='referrer' content='no-referrer'>"
        "<style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;"
        "max-width:560px;margin:72px auto;padding:28px;color:#172033;"
        "background:#f3f6f8}main{background:white;border:1px solid #dce3ea;"
        "border-radius:12px;padding:28px}h2{color:#067647}</style>"
        "</head><body><main><h2>授权结果已返回一键发</h2>"
        "<p>可以关闭此页面并返回一键发。</p></main>"
        "<script>history.replaceState(null,'','/callback/');</script>"
        "</body></html>"
    ).encode("utf-8")


class TikTokOAuthClient:
    """通过系统默认浏览器和随机本机回环端口完成 TikTok 官方授权。"""

    def __init__(
        self,
        store: TikTokCredentialStore,
        *,
        session: requests.Session | None = None,
        browser_open: Callable[[str], Any] = webbrowser.open,
        timeout_seconds: int = 180,
    ) -> None:
        self.store = store
        self.session = session or requests.Session()
        self.browser_open = browser_open
        self.timeout_seconds = max(10, int(timeout_seconds))

    @staticmethod
    def authorization_url(
        config: OAuthClientConfig,
        *,
        redirect_uri: str,
        state: str,
        code_challenge: str,
    ) -> str:
        query = urlencode(
            {
                "client_key": config.client_key,
                "scope": ",".join(TIKTOK_REQUIRED_SCOPES),
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{AUTHORIZATION_ENDPOINT}?{query}"

    def _exchange_code(
        self,
        config: OAuthClientConfig,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> TokenRecord:
        response = self.session.post(
            TOKEN_ENDPOINT,
            data={
                "client_key": config.client_key,
                "client_secret": config.client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise TikTokOAuthError(
                _safe_oauth_error(response, "TikTok OAuth 授权码交换失败")
            )
        try:
            payload = response.json()
        except Exception as exc:
            raise TikTokOAuthError("TikTok OAuth 令牌响应无法读取") from exc
        if not isinstance(payload, dict):
            raise TikTokOAuthError("TikTok OAuth 令牌响应格式错误")
        try:
            return self.store.save_token_response(payload)
        except ValueError as exc:
            raise TikTokOAuthError(str(exc)) from exc

    def authorize_interactively(self) -> TokenRecord:
        config = self.store.load_client()
        state = secrets.token_urlsafe(32)
        code_verifier, code_challenge = _pkce_pair()
        callback: dict[str, str] = {}

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - 标准库回调命名
                parsed = urlparse(self.path)
                if parsed.path != CALLBACK_PATH:
                    self.send_response(404)
                    self.end_headers()
                    return
                query = parse_qs(parsed.query)
                callback["state"] = str((query.get("state") or [""])[0])
                callback["code"] = str((query.get("code") or [""])[0])
                callback["error"] = str((query.get("error") or [""])[0])
                body = _callback_success_page()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                for name, value in CALLBACK_SECURITY_HEADERS.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args: object) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), CallbackHandler)
        redirect_uri = f"http://127.0.0.1:{server.server_port}{CALLBACK_PATH}"
        url = self.authorization_url(
            config,
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=code_challenge,
        )
        try:
            self.browser_open(url)
            deadline = time.monotonic() + self.timeout_seconds
            while not callback and time.monotonic() < deadline:
                server.timeout = min(1.0, max(0.1, deadline - time.monotonic()))
                server.handle_request()
        finally:
            server.server_close()

        if not callback:
            raise TikTokOAuthError("等待 TikTok 官方授权超时，请重新发起授权")
        if not secrets.compare_digest(callback.get("state", ""), state):
            raise TikTokOAuthError("TikTok OAuth 状态校验失败，已拒绝本次授权")
        if callback.get("error"):
            raise TikTokOAuthError(
                f"用户未完成 TikTok 授权：{callback['error'][:100]}"
            )
        code = callback.get("code", "")
        if not code:
            raise TikTokOAuthError("TikTok OAuth 未返回授权码")
        return self._exchange_code(
            config,
            code=code,
            code_verifier=code_verifier,
            redirect_uri=redirect_uri,
        )

    def refresh(self) -> TokenRecord:
        config = self.store.load_client()
        existing = self.store.load_token()
        if existing.refresh_token_expires_at <= datetime.now(timezone.utc):
            raise TikTokOAuthError("TikTok 刷新令牌已到期，需要重新授权")
        response = self.session.post(
            TOKEN_ENDPOINT,
            data={
                "client_key": config.client_key,
                "client_secret": config.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": existing.refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise TikTokOAuthError(
                _safe_oauth_error(response, "TikTok OAuth 令牌刷新失败")
            )
        try:
            payload = response.json()
        except Exception as exc:
            raise TikTokOAuthError("TikTok OAuth 刷新响应无法读取") from exc
        if not isinstance(payload, dict):
            raise TikTokOAuthError("TikTok OAuth 刷新响应格式错误")
        try:
            return self.store.save_token_response(payload, existing=existing)
        except ValueError as exc:
            raise TikTokOAuthError(str(exc)) from exc

    def access_token(self, *, force_refresh: bool = False) -> str:
        token = self.store.load_token()
        if force_refresh or not token.is_access_token_valid():
            token = self.refresh()
        return token.access_token
