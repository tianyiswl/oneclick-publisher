# -*- coding: utf-8 -*-
"""通过服务端 OAuth Broker 完成 Meta 官方授权。"""

from __future__ import annotations

import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import requests

from .credentials import MetaCredentialStore, MetaTokenRecord


CALLBACK_PATH = "/meta/callback"
CALLBACK_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; "
        "style-src 'unsafe-inline'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class MetaOAuthBrokerError(RuntimeError):
    """不回显访问令牌、一次性票据、授权码或 Broker 原始响应。"""


def _safe_error(response: requests.Response, fallback: str) -> str:
    """Broker 返回文本可能回显票据，客户端只返回固定错误。"""

    try:
        payload = response.json()
    except Exception:
        payload = {}
    if isinstance(payload, dict):
        error_code = str(payload.get("code") or "").strip()
        if error_code.isidentifier() and len(error_code) <= 80:
            return f"{fallback}（{error_code}）"
    return fallback


def _response_json(response: requests.Response, fallback: str) -> dict[str, Any]:
    if not 200 <= response.status_code < 300:
        raise MetaOAuthBrokerError(_safe_error(response, fallback))
    try:
        payload = response.json()
    except Exception as exc:
        raise MetaOAuthBrokerError(f"{fallback}：响应无法读取") from exc
    if not isinstance(payload, dict):
        raise MetaOAuthBrokerError(f"{fallback}：响应格式错误")
    if payload.get("ok") is False:
        raise MetaOAuthBrokerError(_safe_error(response, fallback))
    return payload


def _trusted_authorization_url(value: str) -> bool:
    parsed = urlparse(str(value or ""))
    hostname = str(parsed.hostname or "").lower()
    return bool(
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and (
            hostname == "facebook.com"
            or hostname.endswith(".facebook.com")
            or hostname == "instagram.com"
            or hostname.endswith(".instagram.com")
        )
    )


def _callback_page() -> bytes:
    return (
        "<!doctype html><meta charset='utf-8'>"
        "<title>一键发 Meta 授权</title>"
        "<h2>授权结果已返回一键发</h2>"
        "<p>可以关闭此页面并返回一键发。</p>"
        "<script>history.replaceState(null,'','/meta/callback');</script>"
    ).encode("utf-8")


class MetaOAuthBrokerClient:
    """客户端只持有公开 Broker 地址，Meta App Secret 永不进入桌面安装包。"""

    def __init__(
        self,
        store: MetaCredentialStore,
        *,
        session: requests.Session | None = None,
        browser_open: Callable[[str], Any] = webbrowser.open,
        timeout_seconds: int = 240,
    ) -> None:
        self.store = store
        self.session = session or requests.Session()
        self.browser_open = browser_open
        self.timeout_seconds = max(10, int(timeout_seconds))

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        config = self.store.load_broker()
        try:
            response = self.session.post(
                f"{config.broker_url}{path}",
                json=payload,
                headers={"Accept": "application/json"},
                timeout=30,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise MetaOAuthBrokerError("Meta OAuth Broker 暂时无法连接") from exc
        return _response_json(response, "Meta OAuth Broker 请求失败")

    def authorize_interactively(self, profile_name: str = "Meta") -> MetaTokenRecord:
        state = secrets.token_urlsafe(32)
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
                callback["ticket"] = str((query.get("ticket") or [""])[0])
                callback["error"] = str((query.get("error") or [""])[0])
                body = _callback_page()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                for header, value in CALLBACK_SECURITY_HEADERS.items():
                    self.send_header(header, value)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args: object) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), CallbackHandler)
        callback_uri = f"http://127.0.0.1:{server.server_port}{CALLBACK_PATH}"
        try:
            session_payload = self._post(
                "/v1/meta/oauth/sessions",
                {
                    "callbackUri": callback_uri,
                    "state": state,
                    "profileName": str(profile_name or "Meta")[:100],
                },
            )
        except Exception:
            server.server_close()
            raise
        session_id = str(session_payload.get("sessionId") or "").strip()
        authorization_url = str(
            session_payload.get("authorizationUrl") or ""
        ).strip()
        if not session_id or not _trusted_authorization_url(authorization_url):
            server.server_close()
            raise MetaOAuthBrokerError("Meta OAuth Broker 未返回可信的授权会话")

        try:
            self.browser_open(authorization_url)
            deadline = time.monotonic() + self.timeout_seconds
            while not callback and time.monotonic() < deadline:
                server.timeout = min(1.0, max(0.1, deadline - time.monotonic()))
                server.handle_request()
        finally:
            server.server_close()

        if not callback:
            raise MetaOAuthBrokerError("等待 Meta 官方授权超时，请重新发起授权")
        if not secrets.compare_digest(callback.get("state", ""), state):
            raise MetaOAuthBrokerError("Meta OAuth 状态校验失败，已拒绝本次授权")
        if callback.get("error"):
            raise MetaOAuthBrokerError(
                f"用户未完成 Meta 授权：{callback['error'][:100]}"
            )
        ticket = callback.get("ticket", "")
        if not ticket:
            raise MetaOAuthBrokerError("Meta OAuth 回调缺少一次性授权票据")
        exchange = self._post(
            "/v1/meta/oauth/exchange",
            {"sessionId": session_id, "ticket": ticket},
        )
        try:
            return self.store.save_authorization_response(exchange)
        except (ValueError, PermissionError) as exc:
            raise MetaOAuthBrokerError(str(exc)) from exc

    def refresh(self) -> MetaTokenRecord:
        existing = self.store.load_token()
        if not existing.grant_id:
            raise MetaOAuthBrokerError("Meta 授权缺少 Broker Grant ID，需要重新登录")
        refreshed = self._post(
            "/v1/meta/oauth/refresh",
            {"grantId": existing.grant_id},
        )
        try:
            return self.store.save_authorization_response(
                refreshed,
                existing=existing,
            )
        except (ValueError, PermissionError) as exc:
            raise MetaOAuthBrokerError(str(exc)) from exc

    def access_token(self, *, force_refresh: bool = False) -> str:
        token = self.store.load_token()
        if force_refresh or not token.is_access_token_valid():
            token = self.refresh()
        return token.user_access_token
