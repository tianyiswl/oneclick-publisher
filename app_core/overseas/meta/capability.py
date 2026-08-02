# -*- coding: utf-8 -*-
"""Instagram 与 Facebook 官方 Reels 能力适配器。"""

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
from .api import MetaApiClient
from .credentials import META_REQUIRED_SCOPES, MetaCredentialStore, MetaPageAsset
from .oauth import MetaOAuthBrokerClient


INSTAGRAM_OPERATIONS = frozenset(
    {
        OverseasOperation.LOGIN,
        OverseasOperation.AUTHORIZE,
        OverseasOperation.REFRESH_AUTHORIZATION,
        OverseasOperation.UPLOAD_MEDIA,
        OverseasOperation.SET_METADATA,
        OverseasOperation.SAVE_DRAFT,
        OverseasOperation.PUBLISH,
        OverseasOperation.READ_RESULT,
    }
)
FACEBOOK_OPERATIONS = frozenset(
    {
        OverseasOperation.LOGIN,
        OverseasOperation.AUTHORIZE,
        OverseasOperation.REFRESH_AUTHORIZATION,
        OverseasOperation.UPLOAD_MEDIA,
        OverseasOperation.SET_METADATA,
        OverseasOperation.PUBLISH,
        OverseasOperation.READ_RESULT,
    }
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _facebook_state(payload: dict[str, Any]) -> str:
    status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    publishing = (
        status.get("publishing_phase")
        if isinstance(status.get("publishing_phase"), dict)
        else {}
    )
    processing = (
        status.get("processing_phase")
        if isinstance(status.get("processing_phase"), dict)
        else {}
    )
    return str(
        publishing.get("status")
        or status.get("video_status")
        or processing.get("status")
        or "unknown"
    ).strip().lower()


def _instagram_fields_match(
    payload: dict[str, Any],
    request: UploadRequest,
) -> tuple[bool, str]:
    expected_caption_parts = [
        part.strip()
        for part in (request.title, request.description)
        if str(part or "").strip()
    ]
    if request.tags:
        expected_caption_parts.append(
            " ".join(
                f"#{str(tag).strip().lstrip('#')}"
                for tag in request.tags
                if str(tag).strip().lstrip("#")
            )
        )
    expected_caption = "\n\n".join(expected_caption_parts)
    checks = {
        "Reels 类型": str(payload.get("media_product_type") or "").upper()
        == "REELS",
        "文案": str(payload.get("caption") or "") == expected_caption,
        "AI 声明": bool(payload.get("is_ai_generated"))
        == bool(request.ai_generated),
    }
    failed = [label for label, verified in checks.items() if not verified]
    return not failed, (
        "Instagram Reels 类型、文案与 AI 声明已回读"
        if not failed
        else "Instagram 字段回读不一致：" + "、".join(failed)
    )


def _facebook_fields_match(
    payload: dict[str, Any],
    request: UploadRequest,
) -> tuple[bool, str]:
    expected_description_parts = [
        part.strip()
        for part in (request.title, request.description)
        if str(part or "").strip()
    ]
    if request.tags:
        expected_description_parts.append(
            " ".join(
                f"#{str(tag).strip().lstrip('#')}"
                for tag in request.tags
                if str(tag).strip().lstrip("#")
            )
        )
    expected_description = "\n\n".join(expected_description_parts)
    checks = {
        "标题": str(payload.get("title") or "")
        == str(request.title or "").strip()[:255],
        "描述": str(payload.get("description") or "")
        == expected_description,
    }
    failed = [label for label, verified in checks.items() if not verified]
    return not failed, (
        "Facebook Reel 标题与描述已回读"
        if not failed
        else "Facebook 字段回读不一致：" + "、".join(failed)
    )


class _MetaCapabilityBase(OverseasPlatformCapability):
    platform: OverseasPlatform
    operations: frozenset[OverseasOperation]

    def __init__(
        self,
        *,
        store: MetaCredentialStore | None = None,
        oauth: MetaOAuthBrokerClient | None = None,
        api: MetaApiClient | None = None,
        availability: CapabilityAvailability = CapabilityAvailability.DEVELOPMENT,
    ) -> None:
        self.store = store or MetaCredentialStore()
        self.oauth = oauth or MetaOAuthBrokerClient(self.store)
        self.api = api or MetaApiClient(self.store, self.oauth)
        self.availability = availability

    @property
    def descriptor(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            platform=self.platform,
            availability=self.availability,
            operations=self.operations,
        )

    def _target_assets(self, assets: tuple[MetaPageAsset, ...]) -> tuple[MetaPageAsset, ...]:
        if self.platform is OverseasPlatform.INSTAGRAM:
            return tuple(asset for asset in assets if asset.instagram_account_id)
        return assets

    def _reference(self, asset: MetaPageAsset) -> str:
        if self.platform is OverseasPlatform.INSTAGRAM:
            return asset.instagram_account_id
        return asset.page_id

    def _asset_label(self, asset: MetaPageAsset) -> str:
        if self.platform is OverseasPlatform.INSTAGRAM:
            return asset.instagram_username or asset.instagram_account_id
        return asset.page_name or asset.page_id

    async def authorize(self, request: AuthorizationRequest) -> AuthorizationResult:
        return await asyncio.to_thread(self._authorize_sync, request)

    def _authorize_sync(self, request: AuthorizationRequest) -> AuthorizationResult:
        if request.interactive:
            self.oauth.authorize_interactively(request.profile_name)
            login_message = "Meta 官方浏览器授权与 Broker 回调已完成"
        else:
            self.oauth.refresh()
            login_message = "已通过 Meta OAuth Broker 刷新授权"
        granted = set(self.api.permissions())
        missing = set(META_REQUIRED_SCOPES).difference(granted)
        if missing:
            raise PermissionError("Meta 授权缺少 Page 或 Instagram 发布权限")
        assets = self.api.discover_assets()
        candidates = self._target_assets(assets)
        if not candidates:
            target = "Instagram 专业账号" if self.platform is OverseasPlatform.INSTAGRAM else "Facebook Page"
            raise PermissionError(f"当前 Meta 授权未发现可用的{target}")
        account_reference = self._reference(candidates[0]) if len(candidates) == 1 else ""
        target_name = self.descriptor.display_name
        message = (
            f"{target_name} 所需权限已验证，发现 {len(candidates)} 个可选资产"
            + (
                f"：{self._asset_label(candidates[0])}"
                if len(candidates) == 1
                else "，需要在桌面端选择目标账号"
            )
        )
        current = _now()
        return AuthorizationResult(
            platform=self.platform,
            authorized=True,
            account_reference=account_reference,
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
                    message=message,
                    recorded_at=current,
                    reference=account_reference,
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
        granted = set(self.api.permissions())
        if not set(META_REQUIRED_SCOPES).issubset(granted):
            raise PermissionError("刷新后的 Meta 授权缺少所需发布权限")
        assets = self.api.discover_assets()
        expected = str(account_reference or "").strip()
        if expected and expected not in {self._reference(asset) for asset in self._target_assets(assets)}:
            raise PermissionError("刷新后的 Meta 授权不再包含目标资产")
        return AuthorizationResult(
            platform=self.platform,
            authorized=True,
            account_reference=expected,
            evidence=(
                EvidenceRecord(
                    stage=EvidenceStage.AUTHORIZATION,
                    verified=True,
                    message=f"{self.descriptor.display_name} 授权已刷新并重新读取资产",
                    recorded_at=_now(),
                    reference=expected,
                ),
            ),
        )


class InstagramCapability(_MetaCapabilityBase):
    """Instagram 专业账号的本地视频容器上传、显式发布与结果回读。"""

    platform = OverseasPlatform.INSTAGRAM
    operations = INSTAGRAM_OPERATIONS

    async def upload(self, request: UploadRequest) -> UploadResult:
        return await asyncio.to_thread(self._upload_sync, request)

    def _upload_sync(self, request: UploadRequest) -> UploadResult:
        if not request.user_confirmed_upload:
            raise PermissionError("Instagram 上传需要用户明确确认")
        if request.mode not in {PublicationMode.DRAFT, PublicationMode.PUBLIC}:
            raise ValueError("Instagram 首版只允许暂存媒体容器或显式公开发布")
        asset = self.store.page_asset(request.account_reference, instagram=True)
        container_id = self.api.create_instagram_reel(asset, request)
        self.api.upload_instagram_video(container_id, asset, request.video_path)
        self.api.wait_instagram_container(container_id, asset)
        current = _now()
        upload_evidence = EvidenceRecord(
            stage=EvidenceStage.UPLOAD,
            verified=True,
            message="Instagram Reel 已上传到媒体容器并完成处理",
            recorded_at=current,
            reference=container_id,
        )
        if request.mode is PublicationMode.DRAFT:
            return UploadResult(
                platform=self.platform,
                accepted=True,
                operation_reference=(
                    f"container:{asset.instagram_account_id}:{container_id}"
                ),
                evidence=(
                    upload_evidence,
                    EvidenceRecord(
                        stage=EvidenceStage.PUBLICATION,
                        verified=True,
                        message="按用户选择停在媒体容器，未调用 Instagram 公开发布接口",
                        recorded_at=current,
                        reference=container_id,
                    ),
                ),
            )
        media_id = self.api.publish_instagram_reel(container_id, asset)
        media = self.api.read_instagram_media(media_id, asset)
        found = str(media.get("id") or "").strip() == media_id
        fields_verified, fields_message = _instagram_fields_match(
            media,
            request,
        )
        return UploadResult(
            platform=self.platform,
            accepted=found and fields_verified,
            operation_reference=f"media:{asset.instagram_account_id}:{media_id}",
            evidence=(
                upload_evidence,
                EvidenceRecord(
                    stage=EvidenceStage.PUBLICATION,
                    verified=True,
                    message="用户已明确确认，Instagram Reel 公开发布接口已返回媒体 ID",
                    recorded_at=current,
                    reference=media_id,
                ),
                EvidenceRecord(
                    stage=EvidenceStage.RESULT_READBACK,
                    verified=found and fields_verified,
                    message=(
                        fields_message
                        if found
                        else "Instagram 尚未回读到目标 Reel"
                    ),
                    recorded_at=current,
                    reference=media_id,
                ),
            ),
        )

    async def readback(self, operation_reference: str) -> ReadbackResult:
        return await asyncio.to_thread(self._readback_sync, operation_reference)

    def _readback_sync(self, operation_reference: str) -> ReadbackResult:
        raw = str(operation_reference or "").strip()
        parts = raw.split(":", 2)
        if len(parts) != 3 or parts[0] not in {"container", "media"} or not all(parts):
            raise ValueError(
                "Instagram 结果回读需要 container:账号ID:容器ID 或 media:账号ID:媒体ID"
            )
        prefix, account_id, remote_id = parts
        asset = self.store.page_asset(account_id, instagram=True)
        current = _now()
        if prefix == "container":
            payload = self.api.read_instagram_container(remote_id, asset)
            state = str(payload.get("status_code") or "unknown").strip().lower()
            found = bool(payload.get("id"))
        else:
            payload = self.api.read_instagram_media(remote_id, asset)
            state = "published" if str(payload.get("id") or "").strip() == remote_id else "unknown"
            found = bool(payload.get("id"))
        return ReadbackResult(
            platform=self.platform,
            found=found,
            publication_state=state,
            remote_reference=remote_id,
            evidence=(
                EvidenceRecord(
                    stage=EvidenceStage.RESULT_READBACK,
                    verified=found,
                    message=f"Instagram 平台回读状态 {state}",
                    recorded_at=current,
                    reference=remote_id,
                ),
            ),
        )


class FacebookCapability(_MetaCapabilityBase):
    """Facebook Page Reels 的本地文件上传、公开发布与结果回读。"""

    platform = OverseasPlatform.FACEBOOK
    operations = FACEBOOK_OPERATIONS

    async def upload(self, request: UploadRequest) -> UploadResult:
        return await asyncio.to_thread(self._upload_sync, request)

    def _upload_sync(self, request: UploadRequest) -> UploadResult:
        if not request.user_confirmed_upload:
            raise PermissionError("Facebook 上传需要用户明确确认")
        if request.mode is not PublicationMode.PUBLIC:
            raise ValueError("Facebook Page Reel 只能在用户明确选择公开后发布")
        asset = self.store.page_asset(request.account_reference, instagram=False)
        upload = self.api.start_facebook_reel(asset)
        video_id = str(upload.get("video_id") or "").strip()
        self.api.upload_facebook_video(upload, asset, request.video_path)
        self.api.finish_facebook_reel(video_id, asset, request)
        readback = self.api.wait_facebook_reel(video_id, asset)
        state = _facebook_state(readback)
        found = str(readback.get("id") or "").strip() == video_id
        fields_verified, fields_message = _facebook_fields_match(
            readback,
            request,
        )
        current = _now()
        return UploadResult(
            platform=self.platform,
            accepted=found and fields_verified,
            operation_reference=f"{asset.page_id}:{video_id}",
            evidence=(
                EvidenceRecord(
                    stage=EvidenceStage.UPLOAD,
                    verified=True,
                    message="Facebook Reel 视频已完整传输到 Meta",
                    recorded_at=current,
                    reference=video_id,
                ),
                EvidenceRecord(
                    stage=EvidenceStage.PUBLICATION,
                    verified=True,
                    message="用户已明确选择 Public，Facebook Page Reel 发布请求已提交",
                    recorded_at=current,
                    reference=video_id,
                ),
                EvidenceRecord(
                    stage=EvidenceStage.RESULT_READBACK,
                    verified=found and fields_verified,
                    message=(
                        f"Facebook 平台回读状态 {state}；"
                        f"{fields_message}"
                    ),
                    recorded_at=current,
                    reference=video_id,
                ),
            ),
        )

    async def readback(self, operation_reference: str) -> ReadbackResult:
        return await asyncio.to_thread(self._readback_sync, operation_reference)

    def _readback_sync(self, operation_reference: str) -> ReadbackResult:
        raw = str(operation_reference or "").strip()
        page_id, separator, video_id = raw.partition(":")
        if separator != ":" or not page_id or not video_id:
            raise ValueError("Facebook 结果回读需要 PageID:视频ID")
        asset = self.store.page_asset(page_id, instagram=False)
        payload = self.api.read_facebook_reel(video_id, asset)
        found = str(payload.get("id") or "").strip() == video_id
        state = _facebook_state(payload)
        return ReadbackResult(
            platform=self.platform,
            found=found,
            publication_state=state,
            remote_reference=video_id,
            evidence=(
                EvidenceRecord(
                    stage=EvidenceStage.RESULT_READBACK,
                    verified=found,
                    message=f"Facebook 平台回读状态 {state}",
                    recorded_at=_now(),
                    reference=video_id,
                ),
            ),
        )
