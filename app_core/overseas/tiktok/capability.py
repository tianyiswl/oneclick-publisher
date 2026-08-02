# -*- coding: utf-8 -*-
"""TikTok 官方登录与收件箱草稿能力适配器。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from ..capability import OverseasPlatformCapability
from ..models import (
    AuthorizationRequest,
    AuthorizationResult,
    CapabilityAvailability,
    CapabilityDescriptor,
    EvidenceRecord,
    EvidenceStage,
    OverseasOperation,
    OverseasPlatform,
    PublicationMode,
    ReadbackResult,
    UploadRequest,
    UploadResult,
)
from .api import TikTokApiClient
from .credentials import TikTokCredentialStore
from .oauth import TikTokOAuthClient


TIKTOK_OPERATIONS = frozenset(
    {
        OverseasOperation.LOGIN,
        OverseasOperation.AUTHORIZE,
        OverseasOperation.REFRESH_AUTHORIZATION,
        OverseasOperation.UPLOAD_MEDIA,
        OverseasOperation.SAVE_DRAFT,
        OverseasOperation.READ_RESULT,
    }
)

STATUS_STATES = {
    "PROCESSING_UPLOAD": "processing",
    "PROCESSING_DOWNLOAD": "processing",
    "SEND_TO_USER_INBOX": "draft_inbox",
    "PUBLISH_COMPLETE": "published_by_user",
    "FAILED": "failed",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _identity(user: dict[str, Any]) -> tuple[str, str]:
    return (
        str(user.get("open_id") or "").strip(),
        str(user.get("display_name") or "").strip(),
    )


class TikTokCapability(OverseasPlatformCapability):
    """TikTok Login Kit、刷新、收件箱上传和结果回读链路。"""

    def __init__(
        self,
        *,
        store: TikTokCredentialStore | None = None,
        oauth: TikTokOAuthClient | None = None,
        api: TikTokApiClient | None = None,
        availability: CapabilityAvailability = CapabilityAvailability.DEVELOPMENT,
    ) -> None:
        self.store = store or TikTokCredentialStore()
        self.oauth = oauth or TikTokOAuthClient(self.store)
        self.api = api or TikTokApiClient(self.oauth)
        self.availability = availability

    @property
    def descriptor(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            platform=OverseasPlatform.TIKTOK,
            availability=self.availability,
            operations=TIKTOK_OPERATIONS,
        )

    async def authorize(self, request: AuthorizationRequest) -> AuthorizationResult:
        return await asyncio.to_thread(self._authorize_sync, request)

    def _authorize_sync(self, request: AuthorizationRequest) -> AuthorizationResult:
        if request.interactive:
            self.oauth.authorize_interactively()
            login_message = "TikTok 官方浏览器授权回调已完成"
        else:
            self.oauth.refresh()
            login_message = "已使用本机刷新令牌恢复 TikTok 授权"
        user = self.api.current_user()
        open_id, display_name = _identity(user)
        self.store.save_identity(open_id, display_name)
        current = _now()
        return AuthorizationResult(
            platform=OverseasPlatform.TIKTOK,
            authorized=True,
            account_reference=open_id,
            evidence=(
                EvidenceRecord(
                    stage=EvidenceStage.LOGIN,
                    verified=True,
                    message=login_message,
                    recorded_at=current,
                ),
                EvidenceRecord(
                    stage=EvidenceStage.AUTHORIZATION,
                    verified=True,
                    message=(
                        "TikTok 账号读取与视频上传权限已验证，账号身份已回读："
                        f"{display_name or open_id}"
                    ),
                    recorded_at=current,
                    reference=open_id,
                ),
            ),
        )

    async def refresh_authorization(
        self,
        account_reference: str,
    ) -> AuthorizationResult:
        return await asyncio.to_thread(
            self._refresh_authorization_sync,
            account_reference,
        )

    def _refresh_authorization_sync(
        self,
        account_reference: str,
    ) -> AuthorizationResult:
        self.oauth.refresh()
        user = self.api.current_user()
        open_id, display_name = _identity(user)
        expected = str(account_reference or "").strip()
        if expected and open_id != expected:
            raise PermissionError("刷新后的 TikTok 账号与目标账号不一致")
        self.store.save_identity(open_id, display_name)
        return AuthorizationResult(
            platform=OverseasPlatform.TIKTOK,
            authorized=True,
            account_reference=open_id,
            evidence=(
                EvidenceRecord(
                    stage=EvidenceStage.AUTHORIZATION,
                    verified=True,
                    message="TikTok 授权已刷新并重新回读账号身份",
                    recorded_at=_now(),
                    reference=open_id,
                ),
            ),
        )

    async def upload(self, request: UploadRequest) -> UploadResult:
        return await asyncio.to_thread(self._upload_sync, request)

    def _upload_sync(self, request: UploadRequest) -> UploadResult:
        if not request.user_confirmed_upload:
            raise PermissionError("TikTok 上传需要用户明确确认")
        if request.mode is not PublicationMode.DRAFT:
            raise ValueError("TikTok 首版只允许上传到收件箱草稿，不执行直接发布")
        token = self.store.load_token()
        if request.account_reference != token.open_id:
            raise PermissionError("TikTok 上传目标账号与已授权账号不一致")
        uploaded = self.api.upload_video_to_inbox(request.video_path)
        publish_id = str(uploaded.get("publish_id") or "").strip()
        status = str(uploaded.get("status") or "").strip()
        state = STATUS_STATES.get(status, "unknown")
        current = _now()
        inbox_ready = state in {"draft_inbox", "published_by_user"}
        accepted = state not in {"failed", "unknown"}
        return UploadResult(
            platform=OverseasPlatform.TIKTOK,
            accepted=accepted,
            operation_reference=publish_id,
            evidence=(
                EvidenceRecord(
                    stage=EvidenceStage.UPLOAD,
                    verified=True,
                    message="TikTok 已接收全部视频数据并返回 publish_id",
                    recorded_at=current,
                    reference=publish_id,
                ),
                EvidenceRecord(
                    stage=EvidenceStage.PUBLICATION,
                    verified=inbox_ready,
                    message=(
                        "视频已发送到 TikTok 收件箱，仍需用户在 TikTok 内检查、编辑并确认发布"
                        if inbox_ready
                        else f"TikTok 正在处理收件箱上传，当前状态 {status or '未返回'}"
                    ),
                    recorded_at=current,
                    reference=publish_id,
                ),
                EvidenceRecord(
                    stage=EvidenceStage.RESULT_READBACK,
                    verified=accepted,
                    message=f"TikTok 平台回读状态 {status or '未返回'}",
                    recorded_at=current,
                    reference=publish_id,
                ),
            ),
        )

    async def readback(self, operation_reference: str) -> ReadbackResult:
        return await asyncio.to_thread(self._readback_sync, operation_reference)

    def _readback_sync(self, operation_reference: str) -> ReadbackResult:
        publish_id = str(operation_reference or "").strip()
        if not publish_id:
            raise ValueError("TikTok 结果回读需要 publish_id")
        status = self.api.fetch_status(publish_id)
        state = STATUS_STATES.get(status, "unknown")
        found = state != "unknown"
        return ReadbackResult(
            platform=OverseasPlatform.TIKTOK,
            found=found,
            publication_state=state,
            remote_reference=publish_id,
            evidence=(
                EvidenceRecord(
                    stage=EvidenceStage.RESULT_READBACK,
                    verified=found,
                    message=(
                        f"TikTok 状态 {status}；"
                        "published_by_user 仅表示用户已在 TikTok 内完成发布"
                    ),
                    recorded_at=_now(),
                    reference=publish_id,
                ),
            ),
        )
