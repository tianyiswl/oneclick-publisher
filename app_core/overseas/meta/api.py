# -*- coding: utf-8 -*-
"""Meta Graph API v25.0 的资产发现与 Instagram/Facebook Reels 客户端。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from ..models import UploadRequest
from .credentials import MetaCredentialStore, MetaPageAsset
from .oauth import MetaOAuthBrokerClient


SUPPORTED_VIDEO_SUFFIXES = frozenset({".mp4", ".mov"})
MAX_VIDEO_BYTES = 1_000_000_000
TERMINAL_CONTAINER_STATES = frozenset({"FINISHED", "PUBLISHED"})
FAILED_CONTAINER_STATES = frozenset({"ERROR", "EXPIRED"})


class MetaApiError(RuntimeError):
    """不回显 Access Token、Page Token、上传 URL 或 Meta 原始响应。"""


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
                return f"{message[:400]}（{code[:80]}）"
            if message:
                return message[:500]
        message = str(payload.get("message") or "").strip()
        if message:
            return message[:500]
    return fallback


def _response_json(response: requests.Response, fallback: str) -> dict[str, Any]:
    if not 200 <= response.status_code < 300:
        raise MetaApiError(_safe_api_error(response, fallback))
    try:
        payload = response.json()
    except Exception as exc:
        raise MetaApiError(f"{fallback}：响应无法读取") from exc
    if not isinstance(payload, dict):
        raise MetaApiError(f"{fallback}：响应格式错误")
    if isinstance(payload.get("error"), dict):
        raise MetaApiError(_safe_api_error(response, fallback))
    return payload


def _trusted_upload_url(value: str, *, kind: str) -> bool:
    parsed = urlparse(str(value or ""))
    prefix = "/ig-api-upload/" if kind == "instagram" else "/video-upload/"
    return bool(
        parsed.scheme == "https"
        and str(parsed.hostname or "").lower() == "rupload.facebook.com"
        and not parsed.username
        and not parsed.password
        and parsed.path.startswith(prefix)
        and not parsed.fragment
    )


def _validate_video(path: Path) -> tuple[Path, int]:
    video_path = Path(path)
    if not video_path.is_file():
        raise FileNotFoundError(f"Meta 视频文件不存在：{video_path}")
    if video_path.suffix.lower() not in SUPPORTED_VIDEO_SUFFIXES:
        raise ValueError("Meta Reels 上传只支持 MP4 或 MOV")
    size = video_path.stat().st_size
    if size <= 0:
        raise ValueError("Meta 视频文件为空")
    if size > MAX_VIDEO_BYTES:
        raise ValueError("Meta Reels 视频不能超过 1GB")
    return video_path, size


def _caption(request: UploadRequest) -> str:
    parts = [
        part.strip()
        for part in (request.title, request.description)
        if str(part or "").strip()
    ]
    if request.tags:
        parts.append(
            " ".join(
                f"#{str(tag).strip().lstrip('#')}"
                for tag in request.tags
                if str(tag).strip().lstrip("#")
            )
        )
    return "\n\n".join(part for part in parts if part)[:2200]


class MetaApiClient:
    """使用 Broker 签发的用户令牌和 Page Token 调用官方 Graph API。"""

    def __init__(
        self,
        store: MetaCredentialStore,
        oauth: MetaOAuthBrokerClient,
        *,
        session: requests.Session | None = None,
        timeout_seconds: int = 90,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.store = store
        self.oauth = oauth
        self.session = session or requests.Session()
        self.timeout_seconds = max(10, int(timeout_seconds))
        self.sleep = sleep

    @property
    def graph_root(self) -> str:
        config = self.store.load_broker()
        return f"https://graph.facebook.com/{config.graph_version}"

    @property
    def graph_version(self) -> str:
        return self.store.load_broker().graph_version

    def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        token: str,
        **kwargs: Any,
    ) -> requests.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Authorization", f"Bearer {token}")
        url = (
            path_or_url
            if str(path_or_url).startswith("https://")
            else f"{self.graph_root}{path_or_url}"
        )
        try:
            return self.session.request(
                method,
                url,
                headers=headers,
                timeout=self.timeout_seconds,
                allow_redirects=False,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise MetaApiError("Meta Graph API 暂时无法连接") from exc

    def permissions(self) -> tuple[str, ...]:
        response = self._request(
            "GET",
            "/me/permissions",
            token=self.oauth.access_token(),
        )
        payload = _response_json(response, "Meta 权限读取失败")
        data = payload.get("data")
        if not isinstance(data, list):
            raise MetaApiError("Meta 权限响应格式错误")
        return tuple(
            sorted(
                {
                    str(item.get("permission") or "").strip()
                    for item in data
                    if isinstance(item, dict)
                    and str(item.get("status") or "").lower() == "granted"
                    and str(item.get("permission") or "").strip()
                }
            )
        )

    def discover_assets(self) -> tuple[MetaPageAsset, ...]:
        response = self._request(
            "GET",
            "/me/accounts",
            token=self.oauth.access_token(),
            params={
                "fields": (
                    "id,name,tasks,access_token,"
                    "instagram_business_account{id,username,name}"
                ),
                "limit": 100,
            },
        )
        payload = _response_json(response, "Meta Page 与 Instagram 资产读取失败")
        data = payload.get("data")
        if not isinstance(data, list):
            raise MetaApiError("Meta 资产响应格式错误")
        assets: list[MetaPageAsset] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            page_id = str(item.get("id") or "").strip()
            page_token = str(item.get("access_token") or "").strip()
            if not page_id or not page_token:
                continue
            instagram = (
                item.get("instagram_business_account")
                if isinstance(item.get("instagram_business_account"), dict)
                else {}
            )
            tasks_raw = item.get("tasks")
            tasks = tuple(
                sorted(
                    {
                        str(task).strip()
                        for task in (
                            tasks_raw
                            if isinstance(tasks_raw, (list, tuple, set, frozenset))
                            else []
                        )
                        if str(task).strip()
                    }
                )
            )
            assets.append(
                MetaPageAsset(
                    page_id=page_id,
                    page_name=str(item.get("name") or "").strip(),
                    page_access_token=page_token,
                    tasks=tasks,
                    instagram_account_id=str(instagram.get("id") or "").strip(),
                    instagram_username=str(
                        instagram.get("username") or instagram.get("name") or ""
                    ).strip(),
                )
            )
        normalized = tuple(assets)
        self.store.save_assets(normalized)
        return normalized

    def create_instagram_reel(
        self,
        asset: MetaPageAsset,
        request: UploadRequest,
    ) -> str:
        video_path, _ = _validate_video(request.video_path)
        if not asset.instagram_account_id:
            raise PermissionError("目标 Facebook Page 未关联 Instagram 专业账号")
        data: dict[str, str] = {
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": _caption(request),
            "share_to_feed": "true",
        }
        if request.ai_generated:
            data["is_ai_generated"] = "true"
        response = self._request(
            "POST",
            f"/{asset.instagram_account_id}/media",
            token=asset.page_access_token,
            data=data,
        )
        payload = _response_json(response, "Instagram Reel 容器创建失败")
        container_id = str(payload.get("id") or "").strip()
        if not container_id:
            raise MetaApiError("Instagram 未返回媒体容器 ID")
        if video_path.stat().st_size <= 0:
            raise MetaApiError("Instagram 视频文件为空")
        return container_id

    def upload_instagram_video(
        self,
        container_id: str,
        asset: MetaPageAsset,
        video_path: Path,
    ) -> None:
        path, size = _validate_video(video_path)
        upload_url = (
            "https://rupload.facebook.com/ig-api-upload/"
            f"{self.graph_version}/{str(container_id or '').strip()}"
        )
        if not _trusted_upload_url(upload_url, kind="instagram"):
            raise MetaApiError("Instagram 上传地址不可信，已拒绝传输视频")
        with path.open("rb") as handle:
            response = self._request(
                "POST",
                upload_url,
                token=asset.page_access_token,
                headers={
                    "Authorization": f"OAuth {asset.page_access_token}",
                    "Content-Type": "application/octet-stream",
                    "offset": "0",
                    "file_size": str(size),
                },
                data=handle,
            )
        payload = _response_json(response, "Instagram 视频传输失败")
        if payload.get("success") is not True:
            raise MetaApiError("Instagram 未确认视频传输成功")

    def read_instagram_container(
        self,
        container_id: str,
        asset: MetaPageAsset,
    ) -> dict[str, Any]:
        response = self._request(
            "GET",
            f"/{str(container_id or '').strip()}",
            token=asset.page_access_token,
            params={"fields": "id,status_code,status"},
        )
        return _response_json(response, "Instagram 容器状态读取失败")

    def wait_instagram_container(
        self,
        container_id: str,
        asset: MetaPageAsset,
        *,
        attempts: int = 20,
        interval_seconds: float = 3.0,
    ) -> dict[str, Any]:
        last: dict[str, Any] = {}
        for index in range(max(1, int(attempts))):
            last = self.read_instagram_container(container_id, asset)
            state = str(last.get("status_code") or "").strip().upper()
            if state in TERMINAL_CONTAINER_STATES:
                return last
            if state in FAILED_CONTAINER_STATES:
                raise MetaApiError(f"Instagram 视频处理失败：{state}")
            if index + 1 < attempts:
                self.sleep(max(0.0, float(interval_seconds)))
        raise MetaApiError("Instagram 视频处理超时，可稍后回读容器状态")

    def publish_instagram_reel(
        self,
        container_id: str,
        asset: MetaPageAsset,
    ) -> str:
        response = self._request(
            "POST",
            f"/{asset.instagram_account_id}/media_publish",
            token=asset.page_access_token,
            data={"creation_id": str(container_id or "").strip()},
        )
        payload = _response_json(response, "Instagram Reel 发布失败")
        media_id = str(payload.get("id") or "").strip()
        if not media_id:
            raise MetaApiError("Instagram 发布完成但未返回媒体 ID")
        return media_id

    def read_instagram_media(
        self,
        media_id: str,
        asset: MetaPageAsset,
    ) -> dict[str, Any]:
        response = self._request(
            "GET",
            f"/{str(media_id or '').strip()}",
            token=asset.page_access_token,
            params={
                "fields": "id,permalink,media_type,media_product_type,timestamp"
            },
        )
        return _response_json(response, "Instagram Reel 结果回读失败")

    def start_facebook_reel(self, asset: MetaPageAsset) -> dict[str, str]:
        if "CREATE_CONTENT" not in asset.tasks:
            raise PermissionError("当前账号没有目标 Facebook Page 的内容创建权限")
        response = self._request(
            "POST",
            f"/{asset.page_id}/video_reels",
            token=asset.page_access_token,
            data={"upload_phase": "start"},
        )
        payload = _response_json(response, "Facebook Reel 上传会话创建失败")
        video_id = str(payload.get("video_id") or "").strip()
        upload_url = str(payload.get("upload_url") or "").strip()
        if not video_id or not _trusted_upload_url(upload_url, kind="facebook"):
            raise MetaApiError("Facebook 未返回可信的 Reel 上传会话")
        return {"video_id": video_id, "upload_url": upload_url}

    def upload_facebook_video(
        self,
        upload: dict[str, str],
        asset: MetaPageAsset,
        video_path: Path,
    ) -> None:
        path, size = _validate_video(video_path)
        upload_url = str(upload.get("upload_url") or "").strip()
        if not _trusted_upload_url(upload_url, kind="facebook"):
            raise MetaApiError("Facebook 上传地址不可信，已拒绝传输视频")
        with path.open("rb") as handle:
            response = self._request(
                "POST",
                upload_url,
                token=asset.page_access_token,
                headers={
                    "Authorization": f"OAuth {asset.page_access_token}",
                    "Content-Type": "application/octet-stream",
                    "offset": "0",
                    "file_size": str(size),
                },
                data=handle,
            )
        payload = _response_json(response, "Facebook Reel 视频传输失败")
        if payload.get("success") is not True:
            raise MetaApiError("Facebook 未确认视频传输成功")

    def finish_facebook_reel(
        self,
        video_id: str,
        asset: MetaPageAsset,
        request: UploadRequest,
    ) -> None:
        response = self._request(
            "POST",
            f"/{asset.page_id}/video_reels",
            token=asset.page_access_token,
            data={
                "upload_phase": "finish",
                "video_id": str(video_id or "").strip(),
                "video_state": "PUBLISHED",
                "description": _caption(request),
                "title": str(request.title or "").strip()[:255],
            },
        )
        payload = _response_json(response, "Facebook Reel 发布失败")
        if payload.get("success") is not True:
            raise MetaApiError("Facebook 未确认 Reel 发布成功")

    def read_facebook_reel(
        self,
        video_id: str,
        asset: MetaPageAsset,
    ) -> dict[str, Any]:
        response = self._request(
            "GET",
            f"/{str(video_id or '').strip()}",
            token=asset.page_access_token,
            params={"fields": "id,status,permalink_url,created_time"},
        )
        return _response_json(response, "Facebook Reel 状态回读失败")
