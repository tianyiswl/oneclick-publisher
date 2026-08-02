# -*- coding: utf-8 -*-
"""海外平台能力、请求结果与证据状态模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path


class OverseasPlatform(str, Enum):
    """海外增值包的稳定平台标识。"""

    YOUTUBE = "youtube"
    TIKTOK = "tiktok"
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"


PLATFORM_DISPLAY_NAMES = {
    OverseasPlatform.YOUTUBE: "YouTube",
    OverseasPlatform.TIKTOK: "TikTok",
    OverseasPlatform.INSTAGRAM: "Instagram Reels",
    OverseasPlatform.FACEBOOK: "Facebook Reels",
}

PLATFORM_ENTITLEMENT_FEATURES = {
    OverseasPlatform.YOUTUBE: "overseas.youtube",
    OverseasPlatform.TIKTOK: "overseas.tiktok",
    OverseasPlatform.INSTAGRAM: "overseas.meta",
    OverseasPlatform.FACEBOOK: "overseas.meta",
}


class CapabilityAvailability(str, Enum):
    """平台能力是否已达到可交付门槛。"""

    DEVELOPMENT = "development"
    AVAILABLE = "available"

    @property
    def display_text(self) -> str:
        return (
            "开发中"
            if self is CapabilityAvailability.DEVELOPMENT
            else "可用"
        )


class OverseasOperation(str, Enum):
    """平台适配器可声明的原子能力。"""

    LOGIN = "login"
    AUTHORIZE = "authorize"
    REFRESH_AUTHORIZATION = "refresh_authorization"
    UPLOAD_MEDIA = "upload_media"
    SET_THUMBNAIL = "set_thumbnail"
    SET_METADATA = "set_metadata"
    SCHEDULE = "schedule"
    SAVE_DRAFT = "save_draft"
    PUBLISH = "publish"
    READ_RESULT = "read_result"


class PublicationMode(str, Enum):
    """上传后的目标状态；适配器不得把预检误报为其中任一状态。"""

    DRAFT = "draft"
    PRIVATE = "private"
    UNLISTED = "unlisted"
    SCHEDULED = "scheduled"
    PUBLIC = "public"


class EvidenceStage(str, Enum):
    """海外平台纵向链路中必须独立记录的证据阶段。"""

    LOGIN = "login"
    AUTHORIZATION = "authorization"
    UPLOAD = "upload"
    PUBLICATION = "publication"
    RESULT_READBACK = "result_readback"


@dataclass(frozen=True)
class EvidenceRecord:
    """不含凭据的验证证据摘要。

    ``reference`` 只能保存平台返回的作品 ID、任务 ID 或脱敏文件引用，不能保存
    OAuth Token、Cookie、验证码或完整授权响应。
    """

    stage: EvidenceStage
    verified: bool
    message: str
    recorded_at: datetime
    reference: str = ""


@dataclass(frozen=True)
class CapabilityDescriptor:
    """一个平台适配器对外公开的稳定能力描述。"""

    platform: OverseasPlatform
    availability: CapabilityAvailability
    operations: frozenset[OverseasOperation] = field(default_factory=frozenset)

    @property
    def display_name(self) -> str:
        return PLATFORM_DISPLAY_NAMES[self.platform]

    @property
    def entitlement_feature(self) -> str:
        return PLATFORM_ENTITLEMENT_FEATURES[self.platform]

    @property
    def display_label(self) -> str:
        return f"{self.display_name} · {self.availability.display_text}"


@dataclass(frozen=True)
class EntitlementDecision:
    """客户端从已验证授权状态得出的单平台权益判断。"""

    allowed: bool
    code: str
    message: str
    required_feature: str
    source: str = ""


@dataclass(frozen=True)
class PlatformCapabilityState:
    """供桌面界面消费的能力状态与权益状态组合。"""

    descriptor: CapabilityDescriptor
    entitlement: EntitlementDecision

    @property
    def is_usable(self) -> bool:
        return (
            self.descriptor.availability is CapabilityAvailability.AVAILABLE
            and self.entitlement.allowed
        )

    @property
    def display_label(self) -> str:
        return self.descriptor.display_label


@dataclass(frozen=True)
class AuthorizationRequest:
    """发起官方授权所需的非敏感输入。"""

    profile_name: str
    interactive: bool = True


@dataclass(frozen=True)
class AuthorizationResult:
    """授权结果；登录和授权证据必须分阶段记录。"""

    platform: OverseasPlatform
    authorized: bool
    account_reference: str = ""
    evidence: tuple[EvidenceRecord, ...] = ()


@dataclass(frozen=True)
class UploadRequest:
    """平台无关的上传请求，不携带任何凭据。"""

    account_reference: str
    video_path: Path
    title: str
    description: str = ""
    tags: tuple[str, ...] = ()
    thumbnail_path: Path | None = None
    mode: PublicationMode = PublicationMode.PRIVATE
    scheduled_at: datetime | None = None
    made_for_kids: bool = False
    ai_generated: bool = False
    notify_subscribers: bool = True
    share_to_feed: bool = True
    user_confirmed_upload: bool = False


@dataclass(frozen=True)
class UploadResult:
    """上传与目标状态执行结果。"""

    platform: OverseasPlatform
    accepted: bool
    operation_reference: str = ""
    evidence: tuple[EvidenceRecord, ...] = ()


@dataclass(frozen=True)
class ReadbackResult:
    """平台结果回读；用于区分已上传、草稿、定时和已公开。"""

    platform: OverseasPlatform
    found: bool
    publication_state: str = ""
    remote_reference: str = ""
    evidence: tuple[EvidenceRecord, ...] = ()
