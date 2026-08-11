"""一键发本地发布能力层。

本模块属于一键发运行时：它在本进程内完成内容类型转换、平台能力判断和
字段预检，不调用蚁小二客户端、命令行、账号目录或网络接口。

规则来源为已恢复的蚁小二 4.13.25 Schema 与本地 RPA 包中的平台适配约束；
恢复包只用于审计和移植，绝不是一键发运行依赖。真实登录、素材上传、平台
草稿和最终发布必须由一键发自身的执行层在获得用户授权后完成。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ExecutionMode(str, Enum):
    PREPARE = "prepare"
    DRY_RUN = "dry_run"


class ConfirmationPolicy(str, Enum):
    MANUAL_FINAL_CONFIRMATION = "manual_final_confirmation"
    PLATFORM_RECEIPT_REQUIRED = "platform_receipt_required"


class ContentType(str, Enum):
    VIDEO = "video"
    IMAGE_TEXT = "article"
    TEXT = "text"


CONTENT_TYPE_LABELS = {
    ContentType.VIDEO.value: "视频",
    ContentType.IMAGE_TEXT.value: "图文",
    ContentType.TEXT.value: "纯文字",
}

PLATFORM_ALIASES = {
    "公众号": "微信公众号",
    "B站": "哔哩哔哩",
    "Instagram": "Instagram Reels",
    "Facebook": "Facebook Reels",
}


@dataclass(frozen=True)
class PlatformCapability:
    """一个平台内容类型的本地执行契约。"""

    platform: str
    content_type: str
    native_publish_type: str
    requires_assets: bool
    requires_cover: bool
    confirmation_policy: ConfirmationPolicy
    notes: str

    @property
    def content_label(self) -> str:
        return CONTENT_TYPE_LABELS[self.content_type]


def _capability(
    platform: str,
    content_type: ContentType,
    native_publish_type: str,
    *,
    requires_assets: bool = False,
    requires_cover: bool = False,
    notes: str,
) -> PlatformCapability:
    return PlatformCapability(
        platform=platform,
        content_type=content_type.value,
        native_publish_type=native_publish_type,
        requires_assets=requires_assets,
        requires_cover=requires_cover,
        confirmation_policy=ConfirmationPolicy.PLATFORM_RECEIPT_REQUIRED,
        notes=notes,
    )


# 国内六平台与已恢复的四个海外视频通道能力矩阵。
# 海外平台首先开放视频预发布检查；图文与纯文字在没有恢复代码
# 或官方能力证据前保持不支持，不会为了显示全平台而创建空任务。
CAPABILITIES: dict[tuple[str, str], PlatformCapability] = {
    ("小红书", ContentType.VIDEO.value): _capability(
        "小红书", ContentType.VIDEO, "video", requires_assets=True,
        notes="走小红书视频通道。最终发布前需人工确认。",
    ),
    ("小红书", ContentType.IMAGE_TEXT.value): _capability(
        "小红书", ContentType.IMAGE_TEXT, "imageText", requires_assets=True,
        notes="走小红书图文通道，需要图片序列。最终发布前需人工确认。",
    ),
    ("视频号", ContentType.VIDEO.value): _capability(
        "视频号", ContentType.VIDEO, "video", requires_assets=True,
        notes="走视频号视频通道。",
    ),
    ("视频号", ContentType.IMAGE_TEXT.value): _capability(
        "视频号", ContentType.IMAGE_TEXT, "imageText", requires_assets=True,
        notes="走视频号图文通道，需要图片序列。",
    ),
    ("抖音", ContentType.VIDEO.value): _capability(
        "抖音", ContentType.VIDEO, "video", requires_assets=True,
        notes="走抖音视频通道。",
    ),
    ("抖音", ContentType.IMAGE_TEXT.value): _capability(
        "抖音", ContentType.IMAGE_TEXT, "imageText", requires_assets=True,
        notes="走抖音图文通道，需要图片序列。",
    ),
    ("抖音", ContentType.TEXT.value): _capability(
        "抖音", ContentType.TEXT, "article", requires_cover=True,
        notes="走抖音文章通道，平台要求单独封面。",
    ),
    ("快手", ContentType.VIDEO.value): _capability(
        "快手", ContentType.VIDEO, "video", requires_assets=True,
        notes="走快手视频通道。",
    ),
    ("快手", ContentType.IMAGE_TEXT.value): _capability(
        "快手", ContentType.IMAGE_TEXT, "imageText", requires_assets=True,
        notes="走快手图文通道，需要图片序列。",
    ),
    ("哔哩哔哩", ContentType.VIDEO.value): _capability(
        "哔哩哔哩", ContentType.VIDEO, "video", requires_assets=True,
        notes="走 B 站视频通道。",
    ),
    ("哔哩哔哩", ContentType.IMAGE_TEXT.value): _capability(
        "哔哩哔哩", ContentType.IMAGE_TEXT, "article", requires_assets=True,
        notes="图文会转换为 B 站专栏文章，图片序列会写入正文；自定义封面可选。",
    ),
    ("哔哩哔哩", ContentType.TEXT.value): _capability(
        "哔哩哔哩", ContentType.TEXT, "article",
        notes="纯文字会转换为 B 站专栏文章；平台可自动生成封面，也可选填自定义封面。",
    ),
    ("微信公众号", ContentType.IMAGE_TEXT.value): _capability(
        "微信公众号", ContentType.IMAGE_TEXT, "article", requires_assets=True,
        requires_cover=True, notes="图文会转换为公众号文章，至少需要一张封面。",
    ),
    ("微信公众号", ContentType.TEXT.value): _capability(
        "微信公众号", ContentType.TEXT, "article", requires_cover=True,
        notes="纯文字会转换为公众号文章，平台要求单独封面。",
    ),
    ("TikTok", ContentType.VIDEO.value): _capability(
        "TikTok", ContentType.VIDEO, "video", requires_assets=True,
        notes="使用 TikTok Studio 视频通道；支持停在 Post 前预检和确认后的立即发布。",
    ),
    ("YouTube", ContentType.VIDEO.value): _capability(
        "YouTube", ContentType.VIDEO, "video", requires_assets=True,
        notes="使用 YouTube Studio 视频通道；支持停在 SAVE 前预检和确认后的立即发布。",
    ),
    ("Instagram Reels", ContentType.VIDEO.value): _capability(
        "Instagram Reels", ContentType.VIDEO, "video", requires_assets=True,
        notes="使用已恢复的 Meta Business Suite Reels 通道，预检不点击 Share。",
    ),
    ("Facebook Reels", ContentType.VIDEO.value): _capability(
        "Facebook Reels", ContentType.VIDEO, "video", requires_assets=True,
        notes="使用已恢复的 Meta Business Suite Reels 通道，预检不点击 Share。",
    ),
}


def canonical_platform(platform: str) -> str:
    return PLATFORM_ALIASES.get(str(platform or "").strip(), str(platform or "").strip())


def content_type_label(content_type: str) -> str:
    return CONTENT_TYPE_LABELS.get(str(content_type or ""), str(content_type or "未知类型"))


def capability_for(platform: str, content_type: str) -> PlatformCapability | None:
    return CAPABILITIES.get((canonical_platform(platform), str(content_type or "").strip()))


def supports(platform: str, content_type: str) -> bool:
    return capability_for(platform, content_type) is not None


def unsupported_message(platform: str, content_type: str) -> str:
    return (
        f"{platform} 当前不支持{content_type_label(content_type)}发布。"
        "一键发不会创建不可执行任务，请改选可用平台或切换内容类型。"
    )


def supported_platforms(content_type: str) -> tuple[str, ...]:
    order = (
        "抖音", "视频号", "哔哩哔哩", "小红书", "快手", "微信公众号",
        "TikTok", "YouTube", "Instagram Reels", "Facebook Reels",
    )
    return tuple(platform for platform in order if supports(platform, content_type))


def validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """本地字段校验；不访问任何平台，也不读取其它客户端的数据。"""

    requested_platform = str(payload.get("platform") or "").strip()
    content_type = str(payload.get("content_type") or "").strip()
    title = str(payload.get("title") or "").strip()
    content = str(payload.get("content") or payload.get("description") or "").strip()
    assets = list(payload.get("assets") or payload.get("fileList") or [])
    cover = payload.get("cover") or payload.get("coverPath")
    errors: list[str] = []
    capability = capability_for(requested_platform, content_type)

    if capability is None:
        errors.append(unsupported_message(requested_platform, content_type))
        return {
            "mode": ExecutionMode.DRY_RUN.value,
            "valid": False,
            "errors": errors,
            "platform": canonical_platform(requested_platform),
            "contentType": content_type,
            "nextStatus": "不支持",
        }
    if not title:
        errors.append("缺少标题。")
    if not content:
        errors.append("缺少正文或描述。")
    if capability.requires_assets and not assets:
        errors.append(f"{capability.content_label}发布缺少所需素材。")
    if capability.requires_cover and not cover:
        errors.append(f"{capability.platform}{capability.content_label}发布需要单独封面。")

    return {
        "mode": ExecutionMode.DRY_RUN.value,
        "valid": not errors,
        "errors": errors,
        "platform": capability.platform,
        "contentType": capability.content_type,
        "nativePublishType": capability.native_publish_type,
        "confirmationPolicy": capability.confirmation_policy.value,
        "notes": capability.notes,
        "nextStatus": "已准备" if not errors else "待补充",
    }


def prepare_task(payload: dict[str, Any]) -> dict[str, Any]:
    """生成一键发本地准备记录，禁止替代登录、上传、草稿或公开发布。"""

    result = validate_payload(payload)
    return {
        **result,
        "mode": ExecutionMode.PREPARE.value,
        "externalCall": False,
        "message": "已生成一键发本地准备记录；未登录、上传、保存草稿或发布。",
    }
