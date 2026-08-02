# -*- coding: utf-8 -*-
"""YouTube 官方能力的统一适配器实现。"""

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
from .api import YouTubeApiClient
from .credentials import YouTubeCredentialStore
from .oauth import YouTubeOAuthClient


YOUTUBE_OPERATIONS = frozenset(
    {
        OverseasOperation.LOGIN,
        OverseasOperation.AUTHORIZE,
        OverseasOperation.REFRESH_AUTHORIZATION,
        OverseasOperation.UPLOAD_MEDIA,
        OverseasOperation.SET_THUMBNAIL,
        OverseasOperation.SET_METADATA,
        OverseasOperation.SCHEDULE,
        OverseasOperation.SAVE_DRAFT,
        OverseasOperation.PUBLISH,
        OverseasOperation.READ_RESULT,
    }
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _channel_identity(channel: dict[str, Any]) -> tuple[str, str]:
    channel_id = str(channel.get("id") or "").strip()
    snippet = channel.get("snippet") if isinstance(channel.get("snippet"), dict) else {}
    channel_title = str(snippet.get("title") or "").strip()
    return channel_id, channel_title


def _publication_state(video: dict[str, Any]) -> tuple[str, str, str]:
    status = video.get("status") if isinstance(video.get("status"), dict) else {}
    processing = (
        video.get("processingDetails")
        if isinstance(video.get("processingDetails"), dict)
        else {}
    )
    privacy = str(status.get("privacyStatus") or "").strip()
    publish_at = str(status.get("publishAt") or "").strip()
    upload_status = str(status.get("uploadStatus") or "").strip()
    processing_status = str(processing.get("processingStatus") or "").strip()
    if privacy == "private" and publish_at:
        state = "scheduled"
    else:
        state = privacy or upload_status or "unknown"
    return state, upload_status, processing_status


def _metadata_readback(
    video: dict[str, Any],
    request: UploadRequest,
) -> tuple[bool, str]:
    snippet = video.get("snippet") if isinstance(video.get("snippet"), dict) else {}
    status = video.get("status") if isinstance(video.get("status"), dict) else {}
    actual_tags = {
        str(item).strip().lstrip("#")
        for item in snippet.get("tags") or []
        if str(item).strip().lstrip("#")
    }
    expected_tags = {
        str(item).strip().lstrip("#")
        for item in request.tags
        if str(item).strip().lstrip("#")
    }
    checks = {
        "标题": str(snippet.get("title") or "") == str(request.title or "").strip(),
        "描述": str(snippet.get("description") or "") == str(request.description or ""),
        "标签": actual_tags == expected_tags,
        "儿童受众": bool(status.get("selfDeclaredMadeForKids"))
        == bool(request.made_for_kids),
        "AI 声明": bool(status.get("containsSyntheticMedia"))
        == bool(request.ai_generated),
    }
    if request.mode is PublicationMode.SCHEDULED:
        expected_publish_at = (
            request.scheduled_at.astimezone(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
            if request.scheduled_at is not None
            else ""
        )
        checks["定时时间"] = (
            str(status.get("publishAt") or "") == expected_publish_at
        )
    failed = [label for label, verified in checks.items() if not verified]
    return not failed, (
        "YouTube 标题、描述、标签、受众、AI 声明"
        + ("与定时时间" if request.mode is PublicationMode.SCHEDULED else "")
        + "已逐项回读"
        if not failed
        else "YouTube 字段回读不一致：" + "、".join(failed)
    )


class YouTubeCapability(OverseasPlatformCapability):
    """YouTube OAuth、刷新、上传和结果回读的完整纵向链路。"""

    def __init__(
        self,
        *,
        store: YouTubeCredentialStore | None = None,
        oauth: YouTubeOAuthClient | None = None,
        api: YouTubeApiClient | None = None,
        category_id: str = "22",
        availability: CapabilityAvailability = CapabilityAvailability.AVAILABLE,
    ) -> None:
        self.store = store or YouTubeCredentialStore()
        self.oauth = oauth or YouTubeOAuthClient(self.store)
        self.api = api or YouTubeApiClient(self.oauth)
        self.category_id = str(category_id or "22")
        self.availability = availability

    @property
    def descriptor(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            platform=OverseasPlatform.YOUTUBE,
            availability=self.availability,
            operations=YOUTUBE_OPERATIONS,
        )

    async def authorize(self, request: AuthorizationRequest) -> AuthorizationResult:
        return await asyncio.to_thread(self._authorize_sync, request)

    def _authorize_sync(self, request: AuthorizationRequest) -> AuthorizationResult:
        if request.interactive:
            self.oauth.authorize_interactively()
            login_message = "YouTube 官方浏览器授权回调已完成"
        else:
            self.oauth.refresh()
            login_message = "已使用本机刷新令牌恢复 YouTube 授权"
        channel = self.api.current_channel()
        channel_id, channel_title = _channel_identity(channel)
        self.store.save_channel(channel_id, channel_title)
        current = _now()
        return AuthorizationResult(
            platform=OverseasPlatform.YOUTUBE,
            authorized=True,
            account_reference=channel_id,
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
                        f"YouTube 上传与频道只读权限已验证，频道身份已回读："
                        f"{channel_title or channel_id}"
                    ),
                    recorded_at=current,
                    reference=channel_id,
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
        channel = self.api.current_channel()
        channel_id, channel_title = _channel_identity(channel)
        expected = str(account_reference or "").strip()
        if expected and channel_id != expected:
            raise PermissionError("刷新后的 YouTube 频道与目标账号不一致")
        self.store.save_channel(channel_id, channel_title)
        return AuthorizationResult(
            platform=OverseasPlatform.YOUTUBE,
            authorized=True,
            account_reference=channel_id,
            evidence=(
                EvidenceRecord(
                    stage=EvidenceStage.AUTHORIZATION,
                    verified=True,
                    message="YouTube 授权已刷新并重新回读频道身份",
                    recorded_at=_now(),
                    reference=channel_id,
                ),
            ),
        )

    async def upload(self, request: UploadRequest) -> UploadResult:
        return await asyncio.to_thread(self._upload_sync, request)

    def _upload_sync(self, request: UploadRequest) -> UploadResult:
        if not request.user_confirmed_upload:
            raise PermissionError("YouTube 上传需要用户明确确认")
        configured_channel = self.store.load_token().channel_id
        if configured_channel and request.account_reference != configured_channel:
            raise PermissionError("YouTube 上传目标频道与已授权频道不一致")

        uploaded = self.api.upload_video(request, category_id=self.category_id)
        video_id = str(uploaded.get("id") or "").strip()
        current = _now()
        upload_evidence = EvidenceRecord(
            stage=EvidenceStage.UPLOAD,
            verified=True,
            message="YouTube 视频上传已返回平台 ID",
            recorded_at=current,
            reference=video_id,
        )
        thumbnail_evidence: tuple[EvidenceRecord, ...] = ()
        thumbnail_verified = True
        if request.thumbnail_path is not None:
            try:
                self.api.set_thumbnail(video_id, request.thumbnail_path)
                thumbnail_evidence = (
                    EvidenceRecord(
                        stage=EvidenceStage.UPLOAD,
                        verified=True,
                        message="YouTube 自定义封面已设置",
                        recorded_at=current,
                        reference=video_id,
                    ),
                )
            except Exception as exc:
                thumbnail_verified = False
                thumbnail_evidence = (
                    EvidenceRecord(
                        stage=EvidenceStage.UPLOAD,
                        verified=False,
                        message=f"YouTube 视频已创建，但封面设置失败：{str(exc)[:300]}",
                        recorded_at=current,
                        reference=video_id,
                    ),
                )
        try:
            readback = self.api.read_video(video_id)
        except Exception as exc:
            return UploadResult(
                platform=OverseasPlatform.YOUTUBE,
                accepted=False,
                operation_reference=video_id,
                evidence=(
                    upload_evidence,
                    *thumbnail_evidence,
                    EvidenceRecord(
                        stage=EvidenceStage.RESULT_READBACK,
                        verified=False,
                        message=f"YouTube 视频已创建，但结果回读失败：{str(exc)[:300]}",
                        recorded_at=current,
                        reference=video_id,
                    ),
                ),
            )
        if readback is None:
            return UploadResult(
                platform=OverseasPlatform.YOUTUBE,
                accepted=False,
                operation_reference=video_id,
                evidence=(
                    upload_evidence,
                    *thumbnail_evidence,
                    EvidenceRecord(
                        stage=EvidenceStage.RESULT_READBACK,
                        verified=False,
                        message="YouTube 上传后尚未回读到视频",
                        recorded_at=current,
                        reference=video_id,
                    ),
                ),
            )

        actual_state, upload_status, processing_status = _publication_state(readback)
        expected_state = {
            PublicationMode.DRAFT: "private",
            PublicationMode.PRIVATE: "private",
            PublicationMode.UNLISTED: "unlisted",
            PublicationMode.SCHEDULED: "scheduled",
            PublicationMode.PUBLIC: "public",
        }[request.mode]
        publication_verified = actual_state == expected_state
        metadata_verified, metadata_message = _metadata_readback(
            readback,
            request,
        )
        return UploadResult(
            platform=OverseasPlatform.YOUTUBE,
            accepted=(
                publication_verified
                and thumbnail_verified
                and metadata_verified
            ),
            operation_reference=video_id,
            evidence=(
                upload_evidence,
                *thumbnail_evidence,
                EvidenceRecord(
                    stage=EvidenceStage.PUBLICATION,
                    verified=publication_verified,
                    message=(
                        f"目标状态 {expected_state}，平台回读 {actual_state}"
                    ),
                    recorded_at=current,
                    reference=video_id,
                ),
                EvidenceRecord(
                    stage=EvidenceStage.RESULT_READBACK,
                    verified=metadata_verified,
                    message=metadata_message,
                    recorded_at=current,
                    reference=video_id,
                ),
                EvidenceRecord(
                    stage=EvidenceStage.RESULT_READBACK,
                    verified=True,
                    message=(
                        f"上传状态 {upload_status or '未返回'}，"
                        f"处理状态 {processing_status or '未返回'}"
                    ),
                    recorded_at=current,
                    reference=video_id,
                ),
            ),
        )

    async def readback(self, operation_reference: str) -> ReadbackResult:
        return await asyncio.to_thread(self._readback_sync, operation_reference)

    def _readback_sync(self, operation_reference: str) -> ReadbackResult:
        video_id = str(operation_reference or "").strip()
        if not video_id:
            raise ValueError("YouTube 结果回读需要视频 ID")
        video = self.api.read_video(video_id)
        current = _now()
        if video is None:
            return ReadbackResult(
                platform=OverseasPlatform.YOUTUBE,
                found=False,
                remote_reference=video_id,
                evidence=(
                    EvidenceRecord(
                        stage=EvidenceStage.RESULT_READBACK,
                        verified=False,
                        message="YouTube 未返回目标视频",
                        recorded_at=current,
                        reference=video_id,
                    ),
                ),
            )
        state, upload_status, processing_status = _publication_state(video)
        return ReadbackResult(
            platform=OverseasPlatform.YOUTUBE,
            found=True,
            publication_state=state,
            remote_reference=video_id,
            evidence=(
                EvidenceRecord(
                    stage=EvidenceStage.RESULT_READBACK,
                    verified=True,
                    message=(
                        f"YouTube 状态 {state}，上传 {upload_status or '未返回'}，"
                        f"处理 {processing_status or '未返回'}"
                    ),
                    recorded_at=current,
                    reference=video_id,
                ),
            ),
        )
