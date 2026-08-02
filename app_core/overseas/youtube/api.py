# -*- coding: utf-8 -*-
"""不依赖 Google 客户端库的 YouTube Data API v3 客户端。"""

from __future__ import annotations

import mimetypes
import re
import time
from datetime import timezone
from pathlib import Path
from typing import Any, Callable

import requests

from ..models import PublicationMode, UploadRequest
from .oauth import YouTubeOAuthClient


API_ROOT = "https://www.googleapis.com/youtube/v3"
UPLOAD_ROOT = "https://www.googleapis.com/upload/youtube/v3"
DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024
RESUMABLE_GRANULARITY = 256 * 1024
RETRYABLE_STATUS_CODES = {500, 502, 503, 504}


class YouTubeApiError(RuntimeError):
    """不回显 Authorization 请求头或原始 OAuth 响应。"""


def _safe_api_error(response: requests.Response, fallback: str) -> str:
    try:
        payload = response.json()
    except Exception:
        payload = {}
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "").strip()
            errors = error.get("errors")
            reason = ""
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                reason = str(errors[0].get("reason") or "").strip()
            if message and reason:
                return f"{message[:400]}（{reason[:100]}）"
            if message:
                return message[:500]
    return fallback


def _response_json(response: requests.Response, fallback: str) -> dict[str, Any]:
    if not 200 <= response.status_code < 300:
        raise YouTubeApiError(_safe_api_error(response, fallback))
    try:
        payload = response.json()
    except Exception as exc:
        raise YouTubeApiError(f"{fallback}：响应无法读取") from exc
    if not isinstance(payload, dict):
        raise YouTubeApiError(f"{fallback}：响应格式错误")
    return payload


def _mime_type(path: Path, *, video: bool = False) -> str:
    guessed = mimetypes.guess_type(path.name)[0]
    if guessed and (not video or guessed.startswith("video/")):
        return guessed
    return "application/octet-stream"


def _next_offset(range_header: str | None) -> int:
    match = re.fullmatch(r"bytes=0-(\d+)", str(range_header or "").strip())
    return int(match.group(1)) + 1 if match else 0


class YouTubeApiClient:
    """包含频道校验、可恢复上传、封面设置和结果回读。"""

    def __init__(
        self,
        oauth: YouTubeOAuthClient,
        *,
        session: requests.Session | None = None,
        timeout_seconds: int = 60,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        max_retries: int = 5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if chunk_size <= 0 or chunk_size % RESUMABLE_GRANULARITY:
            raise ValueError("YouTube 分片大小必须是 256KB 的正整数倍")
        self.oauth = oauth
        self.session = session or requests.Session()
        self.timeout_seconds = max(10, int(timeout_seconds))
        self.chunk_size = int(chunk_size)
        self.max_retries = max(0, int(max_retries))
        self.sleep = sleep

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        request_headers = dict(headers or {})
        for attempt in range(2):
            token = self.oauth.access_token(force_refresh=attempt == 1)
            request_headers["Authorization"] = f"Bearer {token}"
            response = self.session.request(
                method,
                url,
                headers=request_headers,
                timeout=self.timeout_seconds,
                allow_redirects=False,
                **kwargs,
            )
            if response.status_code != 401 or attempt == 1:
                return response
        raise YouTubeApiError("YouTube API 授权失败")

    def current_channel(self) -> dict[str, Any]:
        response = self._request(
            "GET",
            f"{API_ROOT}/channels",
            params={"part": "id,snippet,status", "mine": "true"},
        )
        payload = _response_json(response, "YouTube 频道身份校验失败")
        items = payload.get("items")
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            raise YouTubeApiError("当前 Google 账号没有可用的 YouTube 频道")
        channel = items[0]
        channel_id = str(channel.get("id") or "").strip()
        if not channel_id:
            raise YouTubeApiError("YouTube 频道身份响应缺少频道 ID")
        return channel

    @staticmethod
    def _upload_resource(
        request: UploadRequest,
        *,
        category_id: str,
    ) -> dict[str, Any]:
        title = str(request.title or "").strip()
        description = str(request.description or "")
        if not title:
            raise ValueError("YouTube 标题不能为空")
        if len(title) > 100:
            raise ValueError("YouTube 标题不能超过 100 个字符")
        if len(description.encode("utf-8")) > 5000:
            raise ValueError("YouTube 描述不能超过 5000 字节")
        if not str(category_id or "").isdigit():
            raise ValueError("YouTube 分类 ID 格式错误")

        privacy_status = {
            PublicationMode.DRAFT: "private",
            PublicationMode.PRIVATE: "private",
            PublicationMode.UNLISTED: "unlisted",
            PublicationMode.SCHEDULED: "private",
            PublicationMode.PUBLIC: "public",
        }[request.mode]
        status: dict[str, Any] = {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": bool(request.made_for_kids),
            "containsSyntheticMedia": bool(request.ai_generated),
        }
        if request.mode is PublicationMode.SCHEDULED:
            if request.scheduled_at is None or request.scheduled_at.tzinfo is None:
                raise ValueError("YouTube 定时发布必须提供带时区的发布时间")
            status["publishAt"] = (
                request.scheduled_at.astimezone(timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )

        snippet: dict[str, Any] = {
            "title": title,
            "description": description,
            "categoryId": str(category_id),
        }
        tags = [str(tag).strip().lstrip("#") for tag in request.tags if str(tag).strip()]
        if tags:
            snippet["tags"] = tags
        return {"snippet": snippet, "status": status}

    def _start_upload_session(
        self,
        request: UploadRequest,
        *,
        category_id: str,
    ) -> tuple[str, str, int]:
        video_path = Path(request.video_path)
        if not video_path.is_file():
            raise FileNotFoundError(f"YouTube 视频文件不存在：{video_path}")
        total_size = video_path.stat().st_size
        if total_size <= 0:
            raise ValueError("YouTube 视频文件为空")
        content_type = _mime_type(video_path, video=True)
        response = self._request(
            "POST",
            f"{UPLOAD_ROOT}/videos",
            params={
                "uploadType": "resumable",
                "part": "snippet,status",
                "notifySubscribers": (
                    "true" if request.notify_subscribers else "false"
                ),
            },
            headers={
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(total_size),
                "X-Upload-Content-Type": content_type,
            },
            json=self._upload_resource(request, category_id=category_id),
        )
        if not 200 <= response.status_code < 300:
            raise YouTubeApiError(
                _safe_api_error(response, "YouTube 可恢复上传会话创建失败")
            )
        upload_url = str(response.headers.get("Location") or "").strip()
        if not upload_url.startswith("https://www.googleapis.com/upload/"):
            raise YouTubeApiError("YouTube 未返回可信的可恢复上传地址")
        return upload_url, content_type, total_size

    def _query_upload_status(
        self,
        upload_url: str,
        *,
        total_size: int,
    ) -> tuple[int, dict[str, Any] | None]:
        response = self._request(
            "PUT",
            upload_url,
            headers={
                "Content-Length": "0",
                "Content-Range": f"bytes */{total_size}",
            },
            data=b"",
        )
        if response.status_code == 308:
            return _next_offset(response.headers.get("Range")), None
        if response.status_code in {200, 201}:
            return total_size, _response_json(response, "YouTube 上传结果读取失败")
        if response.status_code == 404:
            raise YouTubeApiError("YouTube 可恢复上传会话已失效，请重新上传")
        raise YouTubeApiError(_safe_api_error(response, "YouTube 上传状态查询失败"))

    def upload_video(
        self,
        request: UploadRequest,
        *,
        category_id: str = "22",
    ) -> dict[str, Any]:
        if not request.user_confirmed_upload:
            raise PermissionError("YouTube 上传需要用户明确确认")
        upload_url, content_type, total_size = self._start_upload_session(
            request,
            category_id=category_id,
        )
        video_path = Path(request.video_path)
        offset = 0
        retries = 0
        with video_path.open("rb") as handle:
            while offset < total_size:
                handle.seek(offset)
                chunk = handle.read(min(self.chunk_size, total_size - offset))
                if not chunk:
                    raise YouTubeApiError("YouTube 上传读取视频文件失败")
                last_byte = offset + len(chunk) - 1
                try:
                    response = self._request(
                        "PUT",
                        upload_url,
                        headers={
                            "Content-Type": content_type,
                            "Content-Length": str(len(chunk)),
                            "Content-Range": (
                                f"bytes {offset}-{last_byte}/{total_size}"
                            ),
                        },
                        data=chunk,
                    )
                except requests.RequestException:
                    response = None

                if response is not None and response.status_code in {200, 201}:
                    payload = _response_json(response, "YouTube 上传完成响应错误")
                    if not str(payload.get("id") or "").strip():
                        raise YouTubeApiError("YouTube 上传完成但未返回视频 ID")
                    return payload
                if response is not None and response.status_code == 308:
                    next_offset = _next_offset(response.headers.get("Range"))
                    if next_offset <= offset:
                        if retries >= self.max_retries:
                            raise YouTubeApiError("YouTube 分片上传没有取得进展")
                        self.sleep(float(min(2**retries, 8)))
                        offset = next_offset
                        retries += 1
                    else:
                        offset = next_offset
                        retries = 0
                    continue

                retryable = response is None or response.status_code in RETRYABLE_STATUS_CODES
                if not retryable:
                    raise YouTubeApiError(
                        _safe_api_error(response, "YouTube 视频上传失败")
                    )
                if retries >= self.max_retries:
                    raise YouTubeApiError("YouTube 视频上传重试次数已用尽")
                retry_after = 0
                if response is not None:
                    try:
                        retry_after = int(response.headers.get("Retry-After") or 0)
                    except (TypeError, ValueError):
                        retry_after = 0
                self.sleep(float(retry_after or min(2**retries, 8)))
                offset, completed = self._query_upload_status(
                    upload_url,
                    total_size=total_size,
                )
                if completed is not None:
                    if not str(completed.get("id") or "").strip():
                        raise YouTubeApiError("YouTube 上传完成但未返回视频 ID")
                    return completed
                retries += 1
        raise YouTubeApiError("YouTube 视频上传未完成")

    def set_thumbnail(self, video_id: str, thumbnail_path: Path) -> dict[str, Any]:
        path = Path(thumbnail_path)
        if not path.is_file():
            raise FileNotFoundError(f"YouTube 封面文件不存在：{path}")
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("YouTube 自定义封面不能超过 2MB")
        content_type = _mime_type(path)
        if content_type not in {"image/jpeg", "image/png", "application/octet-stream"}:
            raise ValueError("YouTube 自定义封面只支持 JPEG 或 PNG")
        response = self._request(
            "POST",
            f"{UPLOAD_ROOT}/thumbnails/set",
            params={"videoId": video_id, "uploadType": "media"},
            headers={"Content-Type": content_type},
            data=path.read_bytes(),
        )
        return _response_json(response, "YouTube 自定义封面设置失败")

    def read_video(self, video_id: str) -> dict[str, Any] | None:
        response = self._request(
            "GET",
            f"{API_ROOT}/videos",
            params={
                "part": "id,snippet,status,processingDetails",
                "id": str(video_id or "").strip(),
            },
        )
        payload = _response_json(response, "YouTube 视频结果回读失败")
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            return None
        return items[0] if isinstance(items[0], dict) else None
