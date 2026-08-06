# -*- coding: utf-8 -*-
"""小红书图文内容包读取与安全校验。

内容包只负责把本地标题、正文、话题和图片序列带入一键发编辑器；不包含
账号信息，也不触发登录、上传、保存草稿或最终发布。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "oneclick-xhs-content/v1"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
MAX_IMAGES = 20
MAX_TAGS = 10


class XhsContentBundleError(ValueError):
    """小红书图文内容包不符合安全导入规则。"""


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise XhsContentBundleError(f"{label}不能为空")
    return value.strip()


def _bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise XhsContentBundleError(f"{label}必须为 true 或 false")
    return value


def _relative_file(root: Path, value: Any, label: str) -> Path:
    relative = _text(value, label)
    path = Path(relative)
    if path.is_absolute():
        raise XhsContentBundleError(f"{label}必须使用内容包内的相对路径")
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise XhsContentBundleError(f"{label}不能指向内容包外部文件") from exc
    if not resolved.is_file():
        raise XhsContentBundleError(f"{label}不存在：{relative}")
    return resolved


def _tags(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise XhsContentBundleError("tags必须为至少包含一个话题的字符串列表")
    tags: list[str] = []
    seen: set[str] = set()
    for raw in value:
        tag = _text(raw, "tags中的话题").lstrip("#").strip()
        if not tag or tag in seen:
            continue
        tags.append(tag)
        seen.add(tag)
    if not tags:
        raise XhsContentBundleError("tags至少需要一个有效话题")
    if len(tags) > MAX_TAGS:
        raise XhsContentBundleError(f"小红书内容包最多支持 {MAX_TAGS} 个有效话题")
    return tags


def load_xhs_content_bundle(manifest_path: str | Path) -> dict[str, Any]:
    """读取小红书图文内容包，返回可安全带入编辑器的本地内容。"""

    path = Path(manifest_path).expanduser().resolve()
    if not path.is_file():
        raise XhsContentBundleError("请选择内容包中的 manifest.json 文件")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise XhsContentBundleError("manifest.json 必须使用 UTF-8 编码") from exc
    except json.JSONDecodeError as exc:
        raise XhsContentBundleError(f"manifest.json 不是有效 JSON：{exc.msg}") from exc
    if not isinstance(data, Mapping):
        raise XhsContentBundleError("manifest.json 根节点必须是对象")
    if data.get("schemaVersion") != SCHEMA_VERSION:
        raise XhsContentBundleError(f"只支持 {SCHEMA_VERSION} 内容包")
    if data.get("platform") != "小红书":
        raise XhsContentBundleError("内容包 platform 必须为 小红书")
    if data.get("contentType") != "article":
        raise XhsContentBundleError("当前小红书内容包仅支持 contentType=article")
    if _bool(data.get("debugDryRun"), "debugDryRun") is not True:
        raise XhsContentBundleError("内容包必须保持 debugDryRun=true")
    if _bool(data.get("publishAllowed"), "publishAllowed") is not False:
        raise XhsContentBundleError("内容包必须保持 publishAllowed=false")

    title = _text(data.get("title"), "title")
    body_file = _relative_file(path.parent, data.get("bodyFile"), "bodyFile")
    try:
        body = body_file.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError as exc:
        raise XhsContentBundleError("正文文件必须使用 UTF-8 编码") from exc
    if not body:
        raise XhsContentBundleError("正文不能为空")

    images_value = data.get("images")
    if not isinstance(images_value, list) or not images_value:
        raise XhsContentBundleError("images必须为至少包含一张图片的列表")
    if len(images_value) > MAX_IMAGES:
        raise XhsContentBundleError(f"小红书图文内容包最多支持 {MAX_IMAGES} 张图片")
    images = [_relative_file(path.parent, item, "images中的图片") for item in images_value]
    invalid_images = [str(image.name) for image in images if image.suffix.lower() not in IMAGE_SUFFIXES]
    if invalid_images:
        raise XhsContentBundleError("images 只能包含 JPG、PNG 或 WebP 图片：" + "、".join(invalid_images))

    return {
        "title": title,
        "body": body,
        "tags": _tags(data.get("tags")),
        "imagePaths": [str(image) for image in images],
        "sourcePath": str(path),
    }
