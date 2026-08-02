# -*- coding: utf-8 -*-
"""YouTube 桌面应用 OAuth 2.0 + PKCE 授权流程。"""

from __future__ import annotations

import base64
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
    YOUTUBE_REQUIRED_SCOPES,
    OAuthClientConfig,
    TokenRecord,
    YouTubeCredentialStore,
)


AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
CALLBACK_PATH = "/oauth2/callback"


class YouTubeOAuthError(RuntimeError):
    """不包含原始令牌或授权码的 OAuth 错误。"""


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _safe_oauth_error(response: requests.Response, fallback: str) -> str:
    try:
        payload = response.json()
    except Exception:
        payload = {}
    if isinstance(payload, dict):
        description = str(payload.get("error_description") or "").strip()
        code = str(payload.get("error") or "").strip()
        if description:
            return description[:500]
        if code:
            return f"OAuth 错误：{code[:100]}"
    return fallback


class YouTubeOAuthClient:
    """通过系统默认浏览器和随机本机回环端口完成官方授权。"""

    def __init__(
        self,
        store: YouTubeCredentialStore,
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
                "client_id": config.client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(YOUTUBE_REQUIRED_SCOPES),
                "access_type": "offline",
                "prompt": "consent",
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
        payload = {
            "client_id": config.client_id,
            "code": code,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        if config.client_secret:
            payload["client_secret"] = config.client_secret
        response = self.session.post(
            TOKEN_ENDPOINT,
            data=payload,
            timeout=30,
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise YouTubeOAuthError(
                _safe_oauth_error(response, "YouTube OAuth 授权码交换失败")
            )
        try:
            token_payload = response.json()
        except Exception as exc:
            raise YouTubeOAuthError("YouTube OAuth 令牌响应无法读取") from exc
        if not isinstance(token_payload, dict):
            raise YouTubeOAuthError("YouTube OAuth 令牌响应格式错误")
        try:
            return self.store.save_token_response(token_payload)
        except ValueError as exc:
            raise YouTubeOAuthError(str(exc)) from exc

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
                body = (
                    "<!doctype html><meta charset='utf-8'>"
                    "<title>一键发 YouTube 授权</title>"
                    "<h2>授权结果已返回一键发</h2>"
                    "<p>可以关闭此页面并返回一键发。</p>"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args: object) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), CallbackHandler)
        redirect_uri = f"http://127.0.0.1:{server.server_port}{CALLBACK_PATH}"
        authorization_url = self.authorization_url(
            config,
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=code_challenge,
        )
        try:
            self.browser_open(authorization_url)
            deadline = time.monotonic() + self.timeout_seconds
            while not callback and time.monotonic() < deadline:
                server.timeout = min(1.0, max(0.1, deadline - time.monotonic()))
                server.handle_request()
        finally:
            server.server_close()

        if not callback:
            raise YouTubeOAuthError("等待 YouTube 官方授权超时，请重新发起授权")
        if not secrets.compare_digest(callback.get("state", ""), state):
            raise YouTubeOAuthError("YouTube OAuth 状态校验失败，已拒绝本次授权")
        if callback.get("error"):
            raise YouTubeOAuthError(
                f"用户未完成 YouTube 授权：{callback['error'][:100]}"
            )
        code = callback.get("code", "")
        if not code:
            raise YouTubeOAuthError("YouTube OAuth 未返回授权码")
        return self._exchange_code(
            config,
            code=code,
            code_verifier=code_verifier,
            redirect_uri=redirect_uri,
        )

    def refresh(self) -> TokenRecord:
        config = self.store.load_client()
        existing = self.store.load_token()
        if (
            existing.refresh_token_expires_at is not None
            and existing.refresh_token_expires_at <= datetime.now(timezone.utc)
        ):
            raise YouTubeOAuthError("YouTube 刷新令牌已到期，需要重新授权")
        payload = {
            "client_id": config.client_id,
            "refresh_token": existing.refresh_token,
            "grant_type": "refresh_token",
        }
        if config.client_secret:
            payload["client_secret"] = config.client_secret
        response = self.session.post(
            TOKEN_ENDPOINT,
            data=payload,
            timeout=30,
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise YouTubeOAuthError(
                _safe_oauth_error(response, "YouTube OAuth 令牌刷新失败")
            )
        try:
            token_payload = response.json()
        except Exception as exc:
            raise YouTubeOAuthError("YouTube OAuth 刷新响应无法读取") from exc
        if not isinstance(token_payload, dict):
            raise YouTubeOAuthError("YouTube OAuth 刷新响应格式错误")
        try:
            return self.store.save_token_response(
                token_payload,
                existing=existing,
            )
        except ValueError as exc:
            raise YouTubeOAuthError(str(exc)) from exc

    def access_token(self, *, force_refresh: bool = False) -> str:
        token = self.store.load_token()
        if force_refresh or not token.is_access_token_valid():
            token = self.refresh()
        return token.access_token
