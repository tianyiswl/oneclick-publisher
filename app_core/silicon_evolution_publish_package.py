# -*- coding: utf-8 -*-
"""硅基进化 V1.2 冻结包的直发前只读校验。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .silicon_evolution_draft_package import (
    FrozenWechatDraftPackageError,
    load_frozen_wechat_draft_package,
)


class FrozenWechatPublishPackageError(ValueError):
    """冻结包不满足自动直发的额外契约。"""


@dataclass(frozen=True)
class FrozenWechatPublishPackage:
    article_id: str
    package_sha256: str
    title: str
    digest: str
    content_html: str
    cover_path: Path
    body_image_paths: tuple[Path, ...]
    ai_disclosure: dict[str, Any]


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FrozenWechatPublishPackageError(f"{label} 无法读取") from exc
    if not isinstance(value, dict):
        raise FrozenWechatPublishPackageError(f"{label} 必须是对象")
    return value


def _require_quality_passed(receipt: dict[str, Any], article_id: str) -> None:
    gates = receipt.get("gates")
    if receipt.get("article_id") != article_id or not isinstance(gates, list) or not gates:
        raise FrozenWechatPublishPackageError("质量回执与文章身份不一致")
    if any(not isinstance(gate, dict) or gate.get("passed") is not True for gate in gates):
        raise FrozenWechatPublishPackageError("质量回执存在未通过门禁")


def _require_ai_disclosure(
    disclosure: dict[str, Any], body_image_paths: tuple[Path, ...]
) -> dict[str, Any]:
    if disclosure.get("containsAiGeneratedContent") is not True:
        raise FrozenWechatPublishPackageError("自动直发需要明确 AI 图片声明")
    if disclosure.get("contentKinds") != ["image"]:
        raise FrozenWechatPublishPackageError("自动直发仅接受 AI 图片声明")
    if disclosure.get("allowPlatformAutoDeclaration") is not True:
        raise FrozenWechatPublishPackageError("AI 图片声明未允许平台自动展示")
    paths = disclosure.get("assetPaths")
    expected = [str(path.relative_to(path.parents[1])) for path in body_image_paths]
    if paths != expected:
        raise FrozenWechatPublishPackageError("AI 图片声明与正文图片不一致")
    return disclosure


def load_frozen_wechat_publish_package(
    package_dir: Path, *, expected_sha256: str
) -> FrozenWechatPublishPackage:
    """加载已逐文件重验的 V1.2 包，并检查直发专用声明。"""
    try:
        frozen = load_frozen_wechat_draft_package(
            package_dir, expected_sha256=expected_sha256
        )
    except FrozenWechatDraftPackageError as exc:
        raise FrozenWechatPublishPackageError(str(exc)) from exc
    root = Path(package_dir)
    quality = _read_json(root / "quality-receipt.json", "质量回执")
    _require_quality_passed(quality, frozen.article_id)
    disclosure = _read_json(root / "publish-disclosure.json", "直发声明")
    return FrozenWechatPublishPackage(
        article_id=frozen.article_id,
        package_sha256=frozen.package_sha256,
        title=frozen.title,
        digest=frozen.digest,
        content_html=frozen.content_html,
        cover_path=frozen.cover_path,
        body_image_paths=frozen.body_image_paths,
        ai_disclosure=_require_ai_disclosure(disclosure, frozen.body_image_paths),
    )
