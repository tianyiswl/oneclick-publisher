# -*- coding: utf-8 -*-
"""一键发内容包 v1 的本地读取与安全校验。

内容包按视频、图文、文字三种内容形态组织，平台只是可选目标与字段覆盖。
读取过程没有网络、账号、上传、草稿或发表副作用。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "oneclick-content/v1"
CONTENT_TYPES = {"video", "article", "text"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
MAX_ARTICLE_IMAGES = 20
MAX_TAGS = 10
ARTICLE_IMAGE_PLACEMENTS = {
    "after_intro",
    "before_section_1",
    "after_heading",
}
AI_CONTENT_KINDS = {"image", "text", "video", "audio"}


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
    if len(tags) > MAX_TAGS:
        raise ContentBundleError(f"内容包最多支持 {MAX_TAGS} 个有效话题")
    return tags


def _platforms(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ContentBundleError("preferredPlatforms 必须是平台名称列表")
    aliases = {"公众号": "微信公众号", "B站": "哔哩哔哩"}
    return list(dict.fromkeys(aliases.get(item.strip(), item.strip()) for item in value))


def _ai_disclosure(data: Mapping[str, Any], root: Path) -> dict[str, Any]:
    """读取显式 AI 来源标记；缺省时一律不得自动接受平台声明。"""

    value = data.get("aiDisclosure")
    if value is None:
        return {
            "containsAiGeneratedContent": False,
            "contentKinds": [],
            "assetPaths": [],
            "allowPlatformAutoDeclaration": False,
        }
    if not isinstance(value, Mapping):
        raise ContentBundleError("aiDisclosure 必须是对象")
    unexpected = set(value) - {
        "containsAiGeneratedContent",
        "contentKinds",
        "assetPaths",
        "allowPlatformAutoDeclaration",
    }
    if unexpected:
        raise ContentBundleError(
            "aiDisclosure 字段不支持：" + "、".join(sorted(unexpected))
        )
    contains_ai = _bool(
        value.get("containsAiGeneratedContent"),
        "aiDisclosure.containsAiGeneratedContent",
    )
    allow_auto = _bool(
        value.get("allowPlatformAutoDeclaration"),
        "aiDisclosure.allowPlatformAutoDeclaration",
    )
    kinds = value.get("contentKinds")
    if not isinstance(kinds, list) or not all(isinstance(item, str) for item in kinds):
        raise ContentBundleError("aiDisclosure.contentKinds 必须是字符串列表")
    normalized_kinds = list(dict.fromkeys(item.strip().lower() for item in kinds if item.strip()))
    invalid_kinds = [item for item in normalized_kinds if item not in AI_CONTENT_KINDS]
    if invalid_kinds:
        raise ContentBundleError(
            "aiDisclosure.contentKinds 不支持：" + "、".join(invalid_kinds)
        )
    asset_values = value.get("assetPaths")
    if not isinstance(asset_values, list) or not all(
        isinstance(item, str) for item in asset_values
    ):
        raise ContentBundleError("aiDisclosure.assetPaths 必须是字符串列表")
    asset_paths = [
        str(_relative_file(root, item, f"aiDisclosure.assetPaths[{index}]"))
        for index, item in enumerate(asset_values)
    ]
    if allow_auto and not contains_ai:
        raise ContentBundleError(
            "允许平台自动声明时必须明确 containsAiGeneratedContent=true"
        )
    if contains_ai and not normalized_kinds:
        raise ContentBundleError("含 AI 内容时必须声明 aiDisclosure.contentKinds")
    if "image" in normalized_kinds and not asset_paths:
        raise ContentBundleError("声明 AI 图片时必须列出 aiDisclosure.assetPaths")
    return {
        "containsAiGeneratedContent": contains_ai,
        "contentKinds": normalized_kinds,
        "assetPaths": asset_paths,
        "allowPlatformAutoDeclaration": allow_auto,
    }


def _original_declaration(value: Any) -> bool:
    """原创权利不做推断；旧包缺省时安全关闭。"""

    if value is None:
        return False
    return _bool(value, "originalDeclaration")


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


def _assets(
    data: Mapping[str, Any],
    root: Path,
    content_type: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    value = data.get("assets", [])
    if content_type == "text":
        if value not in (None, []):
            raise ContentBundleError("文字包不应包含 assets；请把封面放入 covers")
        return [], []
    if not isinstance(value, list) or not value:
        raise ContentBundleError("视频包和图文包必须包含 assets")
    if content_type == "video" and len(value) != 1:
        raise ContentBundleError("视频包必须且只能包含一个视频素材")
    if content_type == "article" and len(value) > MAX_ARTICLE_IMAGES:
        raise ContentBundleError(f"图文包最多支持 {MAX_ARTICLE_IMAGES} 张图片")

    paths: list[Path] = []
    article_images: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if isinstance(item, str):
            path_value = item
            placement = ""
            anchor = ""
            explicit = False
        elif content_type == "article" and isinstance(item, Mapping):
            unexpected = set(item) - {"path", "placement", "anchor"}
            if unexpected:
                raise ContentBundleError(
                    "assets 图片字段不支持：" + "、".join(sorted(unexpected))
                )
            path_value = item.get("path")
            placement = str(item.get("placement") or "").strip()
            anchor = str(item.get("anchor") or "").strip()
            explicit = bool(placement)
            if placement and placement not in ARTICLE_IMAGE_PLACEMENTS:
                raise ContentBundleError(
                    "assets.placement 必须为 after_intro、before_section_1 或 after_heading"
                )
            if placement == "after_heading" and not anchor:
                raise ContentBundleError("after_heading 必须同时提供 anchor 标题文本")
            if anchor and placement != "after_heading":
                raise ContentBundleError("anchor 仅在 placement=after_heading 时使用")
        else:
            raise ContentBundleError(
                "视频素材必须使用路径字符串；图文素材可使用路径字符串或位置对象"
            )
        path = _relative_file(root, path_value, f"assets[{index}] 中的素材")
        paths.append(path)
        if content_type == "article":
            article_images.append(
                {
                    "path": str(path),
                    "placement": placement,
                    "anchor": anchor,
                    "explicit": explicit,
                }
            )

    allowed = VIDEO_SUFFIXES if content_type == "video" else IMAGE_SUFFIXES
    invalid = [path.name for path in paths if path.suffix.lower() not in allowed]
    if invalid:
        label = "视频" if content_type == "video" else "图片"
        raise ContentBundleError(f"assets 只能包含{label}文件：{'、'.join(invalid)}")

    if content_type == "article":
        unplaced = [item for item in article_images if not item["placement"]]
        has_intro_image = any(
            item["placement"] in {"after_intro", "before_section_1"}
            for item in article_images
        )
        for index, item in enumerate(unplaced):
            if not has_intro_image and index == 0:
                item["placement"] = "after_intro"
                has_intro_image = True
            else:
                item["placement"] = "auto_distribute"
    return [str(path) for path in paths], article_images


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


def _publish_schedule(value: Any) -> dict[str, Any]:
    """读取可选的本地定时设置；这里只校验格式，不触发平台动作。"""

    if value is None:
        return {
            "enabled": False,
            "localTime": "",
            "timezone": "",
        }
    if not isinstance(value, Mapping):
        raise ContentBundleError("publishSchedule 必须是对象")
    unexpected = set(value) - {"enabled", "localTime", "timezone"}
    if unexpected:
        raise ContentBundleError(
            f"publishSchedule 字段不支持：{'、'.join(sorted(unexpected))}"
        )
    enabled = _bool(value.get("enabled"), "publishSchedule.enabled")
    local_time = str(value.get("localTime") or "").strip()
    timezone = str(value.get("timezone") or "").strip()
    if not enabled:
        if local_time or timezone:
            raise ContentBundleError(
                "publishSchedule.enabled=false 时不得携带时间或时区"
            )
        return {
            "enabled": False,
            "localTime": "",
            "timezone": "",
        }
    if not local_time or not timezone:
        raise ContentBundleError(
            "启用 publishSchedule 时必须同时填写 localTime 和 timezone"
        )
    try:
        parsed = datetime.strptime(local_time, "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ContentBundleError(
            "publishSchedule.localTime 必须为 YYYY-MM-DD HH:mm"
        ) from exc
    if parsed.strftime("%Y-%m-%d %H:%M") != local_time:
        raise ContentBundleError(
            "publishSchedule.localTime 必须为 YYYY-MM-DD HH:mm"
        )
    if "/" not in timezone or any(character.isspace() for character in timezone):
        raise ContentBundleError("publishSchedule.timezone 必须为有效 IANA 时区名称")
    return {
        "enabled": True,
        "localTime": local_time,
        "timezone": timezone,
    }


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
    asset_paths, article_images = _assets(data, root, content_type)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "contentType": content_type,
        "title": _text(data.get("title"), "title"),
        "body": _body(data, root),
        "tags": _tags(data.get("tags")),
        "assetPaths": asset_paths,
        "articleImages": article_images,
        "coverPaths": _covers(data, root),
        "preferredPlatforms": _platforms(data.get("preferredPlatforms")),
        "platformOverrides": _overrides(data.get("platformOverrides")),
        "aiDisclosure": _ai_disclosure(data, root),
        "originalDeclaration": _original_declaration(
            data.get("originalDeclaration")
        ),
        "publishSchedule": _publish_schedule(data.get("publishSchedule")),
        "sourcePath": str(path),
    }
