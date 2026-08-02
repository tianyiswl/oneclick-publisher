# -*- coding: utf-8 -*-
"""TikTok 桌面审核界面的安全业务入口。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app_core import activation_service
from app_core import media_service

from ..entitlements import evaluate_overseas_entitlement
from ..models import (
    AuthorizationRequest,
    CapabilityAvailability,
    OverseasPlatform,
    PublicationMode,
    UploadRequest,
)
from ..registry import (
    CapabilityUnavailableError,
    OverseasEntitlementError,
    default_registry,
)
from .api import _upload_plan
from .capability import TikTokCapability
from .credentials import (
    TIKTOK_DATA_DIR,
    TikTokCredentialStore,
    _read_json,
    _write_private_json,
)


SANDBOX_REVIEW_ENV = "ONECLICK_TIKTOK_SANDBOX_REVIEW"
LEGACY_SANDBOX_REVIEW_ENV = "FASHETAI_TIKTOK_SANDBOX_REVIEW"
DEFAULT_OPERATION_PATH = TIKTOK_DATA_DIR / "last-operation.json"


def sandbox_review_enabled() -> bool:
    """只有显式本机验收开关才能临时开放尚未过审的平台。"""

    value = os.getenv(SANDBOX_REVIEW_ENV) or os.getenv(
        LEGACY_SANDBOX_REVIEW_ENV
    )
    return str(value or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _utc_text() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True)
class TikTokReviewStatus:
    """不含 token、client key、secret 或 open_id 的界面状态。"""

    review_mode: bool
    platform_status: str
    entitlement_allowed: bool
    entitlement_message: str
    client_configured: bool
    token_configured: bool
    access_token_valid: bool
    scope_granted: bool
    display_name: str
    actions_enabled: bool
    last_publish_id: str = ""
    last_publication_state: str = ""
    last_video_name: str = ""
    last_updated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class TikTokReviewService:
    """供真实桌面 UI 使用的 TikTok Sandbox/生产共用服务。"""

    def __init__(
        self,
        *,
        store: TikTokCredentialStore | None = None,
        operation_path: Path = DEFAULT_OPERATION_PATH,
        review_mode: bool | None = None,
        license_status_provider: Callable[[], dict[str, Any]] = (
            activation_service.license_status
        ),
    ) -> None:
        self.store = store or TikTokCredentialStore()
        self.operation_path = Path(operation_path)
        self.review_mode = (
            sandbox_review_enabled() if review_mode is None else bool(review_mode)
        )
        self.license_status_provider = license_status_provider

    @property
    def descriptor(self):
        return default_registry().descriptor(OverseasPlatform.TIKTOK)

    def _platform_operation_enabled(self) -> bool:
        return bool(
            self.review_mode
            or self.descriptor.availability is CapabilityAvailability.AVAILABLE
        )

    def _load_operation(self) -> dict[str, Any]:
        if not self.operation_path.is_file():
            return {}
        try:
            payload = _read_json(self.operation_path)
        except (FileNotFoundError, ValueError):
            return {}
        return {
            "publishId": str(payload.get("publishId") or "").strip(),
            "publicationState": str(payload.get("publicationState") or "").strip(),
            "videoName": Path(str(payload.get("videoName") or "")).name,
            "updatedAt": str(payload.get("updatedAt") or "").strip(),
        }

    def _save_operation(
        self,
        *,
        publish_id: str,
        publication_state: str,
        video_name: str = "",
    ) -> None:
        existing = self._load_operation()
        _write_private_json(
            self.operation_path,
            {
                "schemaVersion": 1,
                "publishId": str(publish_id or "").strip(),
                "publicationState": str(publication_state or "").strip(),
                "videoName": Path(video_name or existing.get("videoName") or "").name,
                "updatedAt": _utc_text(),
            },
        )

    def status(self) -> TikTokReviewStatus:
        credentials = self.store.status()
        entitlement = evaluate_overseas_entitlement(
            self.license_status_provider(),
            self.descriptor,
        )
        operation = self._load_operation()
        if self.descriptor.availability is CapabilityAvailability.AVAILABLE:
            platform_status = "可用"
        elif self.review_mode:
            platform_status = "Sandbox 验收"
        else:
            platform_status = "开发中"
        actions_enabled = bool(
            self._platform_operation_enabled()
            and entitlement.allowed
            and credentials.get("clientConfigured")
        )
        return TikTokReviewStatus(
            review_mode=self.review_mode,
            platform_status=platform_status,
            entitlement_allowed=entitlement.allowed,
            entitlement_message=entitlement.message,
            client_configured=bool(credentials.get("clientConfigured")),
            token_configured=bool(credentials.get("tokenConfigured")),
            access_token_valid=bool(credentials.get("accessTokenValid")),
            scope_granted=bool(credentials.get("scopeGranted")),
            display_name=str(credentials.get("displayName") or "").strip(),
            actions_enabled=actions_enabled,
            last_publish_id=str(operation.get("publishId") or ""),
            last_publication_state=str(operation.get("publicationState") or ""),
            last_video_name=str(operation.get("videoName") or ""),
            last_updated_at=str(operation.get("updatedAt") or ""),
        )

    def _capability(self) -> TikTokCapability:
        if not self._platform_operation_enabled():
            raise CapabilityUnavailableError("TikTok 生产应用尚未审核通过")
        decision = evaluate_overseas_entitlement(
            self.license_status_provider(),
            self.descriptor,
        )
        if not decision.allowed:
            raise OverseasEntitlementError(decision.message)
        if not self.store.client_config_path.is_file():
            raise FileNotFoundError("尚未配置 TikTok OAuth 客户凭据")
        return TikTokCapability(
            store=self.store,
            availability=CapabilityAvailability.AVAILABLE,
        )

    @staticmethod
    def _evidence(result: Any) -> list[dict[str, Any]]:
        return [
            {
                "stage": item.stage.value,
                "verified": item.verified,
                "message": item.message,
                "recordedAt": item.recorded_at.isoformat(),
            }
            for item in result.evidence
        ]

    def authorize(self, profile_name: str = "TikTok") -> dict[str, Any]:
        capability = self._capability()
        result = asyncio.run(
            capability.authorize(
                AuthorizationRequest(
                    profile_name=str(profile_name or "TikTok"),
                    interactive=True,
                )
            )
        )
        return {
            "authorized": result.authorized,
            "displayName": self.store.status().get("displayName") or "",
            "evidence": self._evidence(result),
        }

    def refresh_authorization(self) -> dict[str, Any]:
        capability = self._capability()
        token = self.store.load_token()
        result = asyncio.run(capability.refresh_authorization(token.open_id))
        return {
            "authorized": result.authorized,
            "displayName": self.store.status().get("displayName") or "",
            "evidence": self._evidence(result),
        }

    def validate_video(self, video_path: Path) -> dict[str, Any]:
        path = Path(video_path).expanduser().resolve()
        total_size, chunk_size, chunk_count = _upload_plan(path)
        return {
            "path": str(path),
            "name": path.name,
            "sizeBytes": total_size,
            "sizeMb": round(total_size / (1024 * 1024), 2),
            "chunkSize": chunk_size,
            "chunkCount": chunk_count,
        }

    def preview_image(self, video_path: Path) -> str:
        """生成只包含视频首帧的本机预览，不复制或上传原视频。"""

        video = self.validate_video(video_path)
        path = Path(video["path"])
        # Playwright 随包附带的精简 FFmpeg 不能解码常见 H.264 视频；
        # 桌面预览优先使用系统 FFmpeg，不存在时再尝试项目运行时。
        ffmpeg = shutil.which("ffmpeg") or media_service._ffmpeg_path()
        if ffmpeg is None:
            return ""
        fingerprint = hashlib.sha256(
            f"{path}:{path.stat().st_size}:{path.stat().st_mtime_ns}".encode("utf-8")
        ).hexdigest()[:20]
        preview_dir = self.operation_path.parent / "review-previews"
        preview_dir.mkdir(parents=True, exist_ok=True)
        try:
            preview_dir.chmod(0o700)
        except OSError:
            pass
        target = preview_dir / f"{fingerprint}.jpg"
        if not target.is_file():
            try:
                result = subprocess.run(
                    [
                        str(ffmpeg),
                        "-y",
                        "-ss",
                        "00:00:01",
                        "-i",
                        str(path),
                        "-frames:v",
                        "1",
                        "-q:v",
                        "2",
                        str(target),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=20,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                return ""
            if result.returncode != 0 or not target.is_file():
                target.unlink(missing_ok=True)
                return ""
            try:
                target.chmod(0o600)
            except OSError:
                pass
        return str(target)

    def upload_to_inbox(
        self,
        video_path: Path,
        *,
        user_confirmed: bool,
    ) -> dict[str, Any]:
        if not user_confirmed:
            raise PermissionError("上传前必须由用户明确确认")
        video = self.validate_video(video_path)
        capability = self._capability()
        token = self.store.load_token()
        result = asyncio.run(
            capability.upload(
                UploadRequest(
                    account_reference=token.open_id,
                    video_path=Path(video["path"]),
                    title=video["name"],
                    mode=PublicationMode.DRAFT,
                    user_confirmed_upload=True,
                )
            )
        )
        readback = asyncio.run(capability.readback(result.operation_reference))
        self._save_operation(
            publish_id=result.operation_reference,
            publication_state=readback.publication_state,
            video_name=video["name"],
        )
        return {
            "accepted": result.accepted,
            "publishId": result.operation_reference,
            "publicationState": readback.publication_state,
            "videoName": video["name"],
            "evidence": self._evidence(result) + self._evidence(readback),
        }

    def readback(self, publish_id: str = "") -> dict[str, Any]:
        operation = self._load_operation()
        reference = str(publish_id or operation.get("publishId") or "").strip()
        if not reference:
            raise ValueError("尚无可回读的 TikTok 上传任务")
        capability = self._capability()
        result = asyncio.run(capability.readback(reference))
        self._save_operation(
            publish_id=reference,
            publication_state=result.publication_state,
            video_name=str(operation.get("videoName") or ""),
        )
        return {
            "found": result.found,
            "publishId": reference,
            "publicationState": result.publication_state,
            "videoName": str(operation.get("videoName") or ""),
            "evidence": self._evidence(result),
        }
