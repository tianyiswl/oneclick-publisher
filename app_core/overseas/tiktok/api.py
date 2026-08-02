# -*- coding: utf-8 -*-
"""TikTok Content Posting API 收件箱上传客户端。"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from .oauth import TikTokOAuthClient


API_ROOT = "https://open.tiktokapis.com"
MIN_CHUNK_SIZE = 5_000_000
DEFAULT_CHUNK_SIZE = 10_000_000
MAX_CHUNK_SIZE = 64_000_000
MAX_FINAL_CHUNK_SIZE = 128_000_000
MAX_FILE_SIZE = 4_000_000_000
MAX_CHUNK_COUNT = 1000
SUPPORTED_SUFFIXES = frozenset({".mp4", ".webm", ".mov"})


class TikTokApiError(RuntimeError):
    """不回显 Authorization、上传地址或原始 OAuth 响应。"""


def _safe_api_error(response: requests.Response, fallback: str) -> str:
    try:
        payload = response.json()
    except Exception:
        payload = {}
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "").strip()
            code = str(error.get("code") or "").strip()
            if message and code:
                return f"{message[:400]}（{code[:100]}）"
            if message:
                return message[:500]
            if code and code != "ok":
                return f"TikTok API 错误：{code[:100]}"
    return fallback


def _response_json(response: requests.Response, fallback: str) -> dict[str, Any]:
    if not 200 <= response.status_code < 300:
        raise TikTokApiError(_safe_api_error(response, fallback))
    try:
        payload = response.json()
    except Exception as exc:
        raise TikTokApiError(f"{fallback}：响应无法读取") from exc
    if not isinstance(payload, dict):
        raise TikTokApiError(f"{fallback}：响应格式错误")
    error = payload.get("error")
    if isinstance(error, dict):
        code = str(error.get("code") or "").strip()
        if code and code != "ok":
            raise TikTokApiError(_safe_api_error(response, fallback))
    return payload


def _upload_url_is_trusted(value: str) -> bool:
    parsed = urlparse(value)
    hostname = str(parsed.hostname or "").lower()
    return bool(
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and (hostname == "tiktokapis.com" or hostname.endswith(".tiktokapis.com"))
    )


def _upload_plan(path: Path) -> tuple[int, int, int]:
    video_path = Path(path)
    if not video_path.is_file():
        raise FileNotFoundError(f"TikTok 视频文件不存在：{video_path}")
    if video_path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError("TikTok 收件箱上传只支持 MP4、WebM 或 MOV")
    total_size = video_path.stat().st_size
    if total_size <= 0:
        raise ValueError("TikTok 视频文件为空")
    if total_size > MAX_FILE_SIZE:
        raise ValueError("TikTok 视频文件不能超过 4GB")
    if total_size <= MAX_CHUNK_SIZE:
        return total_size, total_size, 1
    chunk_size = DEFAULT_CHUNK_SIZE
    chunk_count = total_size // chunk_size
    if chunk_count <= 0 or chunk_count > MAX_CHUNK_COUNT:
        raise ValueError("TikTok 视频分片数量超出平台限制")
    final_chunk_size = total_size - chunk_size * (chunk_count - 1)
    if not MIN_CHUNK_SIZE <= chunk_size <= MAX_CHUNK_SIZE:
        raise ValueError("TikTok 视频分片大小不符合平台限制")
    if final_chunk_size > MAX_FINAL_CHUNK_SIZE:
        raise ValueError("TikTok 最后一个视频分片过大")
    return total_size, chunk_size, chunk_count


class TikTokApiClient:
    """账号回读、收件箱上传初始化、媒体传输和发布状态回读。"""

    def __init__(
        self,
        oauth: TikTokOAuthClient,
        *,
        session: requests.Session | None = None,
        timeout_seconds: int = 60,
    ) -> None:
        self.oauth = oauth
        self.session = session or requests.Session()
        self.timeout_seconds = max(10, int(timeout_seconds))

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        for attempt in range(2):
            token = self.oauth.access_token(force_refresh=attempt == 1)
            headers["Authorization"] = f"Bearer {token}"
            response = self.session.request(
                method,
                f"{API_ROOT}{path}",
                headers=headers,
                timeout=self.timeout_seconds,
                allow_redirects=False,
                **kwargs,
            )
            if response.status_code != 401 or attempt == 1:
                return response
        raise TikTokApiError("TikTok API 授权失败")

    def current_user(self) -> dict[str, Any]:
        response = self._request(
            "GET",
            "/v2/user/info/",
            params={"fields": "open_id,display_name,avatar_url"},
        )
        payload = _response_json(response, "TikTok 账号身份校验失败")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        if not str(user.get("open_id") or "").strip():
            raise TikTokApiError("TikTok 账号身份响应缺少 open_id")
        return user

    def initialize_inbox_upload(self, video_path: Path) -> dict[str, Any]:
        total_size, chunk_size, chunk_count = _upload_plan(video_path)
        response = self._request(
            "POST",
            "/v2/post/publish/inbox/video/init/",
            headers={"Content-Type": "application/json; charset=UTF-8"},
            json={
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": total_size,
                    "chunk_size": chunk_size,
                    "total_chunk_count": chunk_count,
                }
            },
        )
        payload = _response_json(response, "TikTok 收件箱上传初始化失败")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        publish_id = str(data.get("publish_id") or "").strip()
        upload_url = str(data.get("upload_url") or "").strip()
        if not publish_id or not _upload_url_is_trusted(upload_url):
            raise TikTokApiError("TikTok 未返回可信的上传任务信息")
        return {
            "publish_id": publish_id,
            "upload_url": upload_url,
            "total_size": total_size,
            "chunk_size": chunk_size,
            "chunk_count": chunk_count,
        }

    def transfer_video(self, video_path: Path, upload: dict[str, Any]) -> None:
        upload_url = str(upload.get("upload_url") or "").strip()
        if not _upload_url_is_trusted(upload_url):
            raise TikTokApiError("TikTok 上传地址不可信，已拒绝传输视频")
        total_size = int(upload.get("total_size") or 0)
        chunk_size = int(upload.get("chunk_size") or 0)
        chunk_count = int(upload.get("chunk_count") or 0)
        actual_size, expected_chunk_size, expected_count = _upload_plan(video_path)
        if (total_size, chunk_size, chunk_count) != (
            actual_size,
            expected_chunk_size,
            expected_count,
        ):
            raise TikTokApiError("TikTok 上传计划与本地视频不一致")
        content_type = mimetypes.guess_type(Path(video_path).name)[0] or "video/mp4"
        offset = 0
        with Path(video_path).open("rb") as handle:
            for index in range(chunk_count):
                remaining = total_size - offset
                requested = remaining if index == chunk_count - 1 else chunk_size
                chunk = handle.read(requested)
                if len(chunk) != requested:
                    raise TikTokApiError("TikTok 上传读取视频文件失败")
                last_byte = offset + len(chunk) - 1
                try:
                    response = self.session.put(
                        upload_url,
                        headers={
                            "Content-Type": content_type,
                            "Content-Length": str(len(chunk)),
                            "Content-Range": (
                                f"bytes {offset}-{last_byte}/{total_size}"
                            ),
                        },
                        data=chunk,
                        timeout=self.timeout_seconds,
                        allow_redirects=False,
                    )
                except requests.RequestException:
                    # 上传地址通常携带临时签名，异常信息不得把完整 URL 回显给调用方。
                    raise TikTokApiError("TikTok 视频分片传输发生网络错误") from None
                expected_status = 201 if index == chunk_count - 1 else 206
                if response.status_code != expected_status:
                    raise TikTokApiError("TikTok 视频分片传输失败")
                offset += len(chunk)
        if offset != total_size:
            raise TikTokApiError("TikTok 视频上传未完整传输")

    def upload_video_to_inbox(self, video_path: Path) -> dict[str, Any]:
        upload = self.initialize_inbox_upload(video_path)
        self.transfer_video(video_path, upload)
        return {
            "publish_id": upload["publish_id"],
            "status": self.fetch_status(upload["publish_id"]),
        }

    def fetch_status(self, publish_id: str) -> str:
        normalized = str(publish_id or "").strip()
        if not normalized:
            raise ValueError("TikTok 状态回读需要 publish_id")
        response = self._request(
            "POST",
            "/v2/post/publish/status/fetch/",
            headers={"Content-Type": "application/json; charset=UTF-8"},
            json={"publish_id": normalized},
        )
        payload = _response_json(response, "TikTok 发布状态回读失败")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        status = str(data.get("status") or "").strip()
        if not status:
            raise TikTokApiError("TikTok 发布状态响应缺少 status")
        return status
