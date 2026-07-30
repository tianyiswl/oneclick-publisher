# -*- coding: utf-8 -*-
"""一键发内容包 v1 的本地读取与安全校验。

内容包按视频、图文、文字三种内容形态组织，平台只是可选目标与字段覆盖。
读取过程没有网络、账号、上传、草稿或发表副作用。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "oneclick-content/v1"
CONTENT_TYPES = {"video", "article", "text"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
MAX_ARTICLE_IMAGES = 20


class ContentBundleError(ValueError):
    """内容包未满足一键发的本地安全导入规则。"""


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContentBundleError(f"{label}不能为空")
    return value.strip()


def _bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ContentBundleError(f"{label}必须为 true 或 false")
    return value


def _relative_file(root: Path, value: Any, label: str) -> Path:
    relative = _text(value, label)
    candidate = Path(relative)
    if candidate.is_absolute():
        raise ContentBundleError(f"{label}必须使用内容包内的相对路径")
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ContentBundleError(f"{label}不能指向内容包外部文件") from exc
    if not resolved.is_file():
        raise ContentBundleError(f"{label}不存在：{relative}")
    return resolved


def _body(data: Mapping[str, Any], root: Path) -> str:
    inline = data.get("body")
    body_file = data.get("bodyFile")
    if inline is not None and body_file is not None:
        raise ContentBundleError("body 与 bodyFile 只能填写一个")
    if body_file is not None:
        path = _relative_file(root, body_file, "bodyFile")
        try:
            return _text(path.read_text(encoding="utf-8"), "正文")
        except UnicodeDecodeError as exc:
            raise ContentBundleError("正文文件必须使用 UTF-8 编码") from exc
    return _text(inline, "body")


def _tags(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ContentBundleError("tags 必须是字符串列表")
    tags: list[str] = []
    seen = set()
    for value in value:
        tag = value.strip().lstrip("#").strip()
        if tag and tag not in seen:
            tags.append(tag)
            seen.add(tag)
    return tags


def _platforms(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ContentBundleError("preferredPlatforms 必须是平台名称列表")
    aliases = {"公众号": "微信公众号", "B站": "哔哩哔哩"}
    return list(dict.fromkeys(aliases.get(item.strip(), item.strip()) for item in value))


def _covers(data: Mapping[str, Any], root: Path) -> dict[str, str]:
    value = data.get("covers")
    if not isinstance(value, Mapping):
        raise ContentBundleError("covers 必须包含至少一张 3:4 或 4:3 封面")
    covers: dict[str, str] = {}
    for ratio in ("3:4", "4:3"):
        if value.get(ratio) is None:
            continue
        path = _relative_file(root, value[ratio], f"covers.{ratio}")
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            raise ContentBundleError(f"covers.{ratio} 必须是 JPG、PNG 或 WebP 图片")
        covers[ratio] = str(path)
    if not covers:
        raise ContentBundleError("covers 至少需要一张 3:4 或 4:3 封面")
    return covers


def _assets(data: Mapping[str, Any], root: Path, content_type: str) -> list[str]:
    value = data.get("assets", [])
    if content_type == "text":
        if value not in (None, []):
            raise ContentBundleError("文字包不应包含 assets；请把封面放入 covers")
        return []
    if not isinstance(value, list) or not value:
        raise ContentBundleError("视频包和图文包必须包含 assets")
    if content_type == "video" and len(value) != 1:
        raise ContentBundleError("视频包必须且只能包含一个视频素材")
    if content_type == "article" and len(value) > MAX_ARTICLE_IMAGES:
        raise ContentBundleError(f"图文包最多支持 {MAX_ARTICLE_IMAGES} 张图片")
    paths = [_relative_file(root, item, "assets 中的素材") for item in value]
    allowed = VIDEO_SUFFIXES if content_type == "video" else IMAGE_SUFFIXES
    invalid = [path.name for path in paths if path.suffix.lower() not in allowed]
    if invalid:
        label = "视频" if content_type == "video" else "图片"
        raise ContentBundleError(f"assets 只能包含{label}文件：{'、'.join(invalid)}")
    return [str(path) for path in paths]


def _overrides(value: Any) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ContentBundleError("platformOverrides 必须是对象")
    result: dict[str, dict[str, Any]] = {}
    for platform, override in value.items():
        if not isinstance(platform, str) or not platform.strip() or not isinstance(override, Mapping):
            raise ContentBundleError("platformOverrides 的平台名和配置必须有效")
        allowed = {"title", "body", "tags"}
        unexpected = set(override) - allowed
        if unexpected:
            raise ContentBundleError(f"{platform} 覆盖字段不支持：{'、'.join(sorted(unexpected))}")
        normalized: dict[str, Any] = {}
        if "title" in override:
            normalized["title"] = _text(override["title"], f"{platform}.title")
        if "body" in override:
            normalized["body"] = _text(override["body"], f"{platform}.body")
        if "tags" in override:
            normalized["tags"] = _tags(override["tags"])
        result[platform.strip()] = normalized
    return result


def load_content_bundle(manifest_path: str | Path, *, expected_type: str | None = None) -> dict[str, Any]:
    """读取内容包并返回可安全带入编辑器的规范载荷。"""

    path = Path(manifest_path).expanduser().resolve()
    if not path.is_file() or path.name != "manifest.json":
        raise ContentBundleError("请选择内容包中的 manifest.json 文件")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise ContentBundleError("manifest.json 必须使用 UTF-8 编码") from exc
    except json.JSONDecodeError as exc:
        raise ContentBundleError(f"manifest.json 不是有效 JSON：{exc.msg}") from exc
    if not isinstance(data, Mapping):
        raise ContentBundleError("manifest.json 根节点必须是对象")
    if data.get("schemaVersion") != SCHEMA_VERSION:
        raise ContentBundleError(f"只支持 {SCHEMA_VERSION} 内容包")
    content_type = data.get("contentType")
    if content_type not in CONTENT_TYPES:
        raise ContentBundleError("contentType 必须为 video、article 或 text")
    if expected_type and content_type != expected_type:
        labels = {"video": "视频包", "article": "图文包", "text": "文字包"}
        raise ContentBundleError(f"当前入口只支持导入{labels[expected_type]}")
    if _bool(data.get("debugDryRun"), "debugDryRun") is not True:
        raise ContentBundleError("内容包必须保持 debugDryRun=true")
    if _bool(data.get("publishAllowed"), "publishAllowed") is not False:
        raise ContentBundleError("内容包必须保持 publishAllowed=false")
    root = path.parent.resolve()
    return {
        "schemaVersion": SCHEMA_VERSION,
        "contentType": content_type,
        "title": _text(data.get("title"), "title"),
        "body": _body(data, root),
        "tags": _tags(data.get("tags")),
        "assetPaths": _assets(data, root, content_type),
        "coverPaths": _covers(data, root),
        "preferredPlatforms": _platforms(data.get("preferredPlatforms")),
        "platformOverrides": _overrides(data.get("platformOverrides")),
        "sourcePath": str(path),
    }
