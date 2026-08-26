# -*- coding: utf-8 -*-
"""硅基进化公众号自动直发的本机载荷契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .silicon_evolution_publish_package import FrozenWechatPublishPackage


class SiliconEvolutionAutoPublishError(ValueError):
    """自动直发未满足账号或冻结包边界。"""


@dataclass(frozen=True)
class AutoPublishProfile:
    project_id: str
    account_id: int
    account_display_name: str
    enabled: bool

    def __post_init__(self) -> None:
        if self.project_id != "silicon-evolution":
            raise SiliconEvolutionAutoPublishError("自动直发项目不匹配")
        if self.account_id <= 0 or not self.account_display_name.strip():
            raise SiliconEvolutionAutoPublishError("自动直发账号无效")


def build_silicon_evolution_payload(
    package: FrozenWechatPublishPackage,
    profile: AutoPublishProfile,
    *,
    account_id: int,
    mode: Literal["preflight", "publish"],
) -> dict[str, Any]:
    """由已冻结包生成执行器载荷；发布策略固定为不群发、不定时。"""
    if not profile.enabled:
        raise SiliconEvolutionAutoPublishError("自动直发档案未启用")
    if account_id != profile.account_id:
        raise SiliconEvolutionAutoPublishError("自动直发账号与档案不一致")
    if mode not in {"preflight", "publish"}:
        raise SiliconEvolutionAutoPublishError("自动直发模式无效")
    return {
        "type": 10,
        "contentType": "article",
        "title": package.title,
        "description": package.content_html,
        "fileList": [str(path) for path in package.body_image_paths],
        "accountIds": [profile.account_id],
        "accountDisplayNames": [profile.account_display_name],
        "coverPath": str(package.cover_path),
        "runtimeMode": mode,
        "debugDryRun": mode == "preflight",
        "saveDraftOnly": False,
        "wechatGroupNotification": False,
        "enableTimer": False,
        "scheduleTime": None,
        "aiGenerated": True,
        "aiDeclarationExplicitlyConfirmed": mode == "publish",
        "aiDisclosure": dict(package.ai_disclosure),
        "siliconEvolutionArticleId": package.article_id,
        "siliconEvolutionPackageSha256": package.package_sha256,
    }
