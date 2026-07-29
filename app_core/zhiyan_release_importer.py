# -*- coding: utf-8 -*-
"""知言发布包安全导入器。

该模块只负责把知言「发布台配置 JSON」转换为可审计的抖音预发布载荷。
它不导入、也不调用任何上传或发布运行时。输出始终强制为干跑且仅存草稿。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence, Union


SCHEMA_VERSION = "zhiyan-prepublish/v1"
DOUYIN_PLATFORM_TYPE = 3
MAX_DOUYIN_TAGS = 5


class ZhiyanReleaseImportError(ValueError):
    """知言发布包无法安全导入。"""


def _required_text(data: Mapping[str, Any], key: str, label: str, *, preserve: bool = False) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ZhiyanReleaseImportError(f"{label}不能为空")
    return value if preserve else value.strip()


def _required_absolute_file(data: Mapping[str, Any], key: str, label: str) -> Path:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ZhiyanReleaseImportError(f"{label}路径不能为空")

    path = Path(value)
    if not path.is_absolute():
        raise ZhiyanReleaseImportError(f"{label}必须使用绝对路径：{value}")
    if not path.exists():
        raise ZhiyanReleaseImportError(f"{label}不存在：{value}")
    if not path.is_file():
        raise ZhiyanReleaseImportError(f"{label}不是文件：{value}")
    return path.resolve()


def _parse_bool(value: Any, label: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ZhiyanReleaseImportError(f"{label}必须是明确的布尔值")


def _assert_safe_flags(data: Mapping[str, Any], scope: str) -> None:
    if "publishAllowed" in data and _parse_bool(data["publishAllowed"], f"{scope}.publishAllowed"):
        raise ZhiyanReleaseImportError(f"{scope}不得启用正式发布（publishAllowed=true）")

    if "saveDraftOnly" in data and not _parse_bool(data["saveDraftOnly"], f"{scope}.saveDraftOnly"):
        raise ZhiyanReleaseImportError(f"{scope}必须保持仅存草稿（saveDraftOnly=true）")

    if "debugDryRun" in data and not _parse_bool(data["debugDryRun"], f"{scope}.debugDryRun"):
        raise ZhiyanReleaseImportError(f"{scope}必须保持干跑（debugDryRun=true）")


def _find_douyin_platform(config: Mapping[str, Any]) -> Mapping[str, Any]:
    platforms = config.get("platforms")
    if not isinstance(platforms, list):
        raise ZhiyanReleaseImportError("platforms 必须是平台配置列表")

    matches = []
    for item in platforms:
        if not isinstance(item, Mapping):
            raise ZhiyanReleaseImportError("platforms 中的每个平台都必须是对象")
        name = item.get("name")
        if isinstance(name, str) and name.strip() == "抖音":
            matches.append(item)

    if not matches:
        raise ZhiyanReleaseImportError("发布包中缺少抖音平台配置")
    if len(matches) > 1:
        raise ZhiyanReleaseImportError("发布包中只能有一个抖音平台配置")
    return matches[0]


def _normalize_tags(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_values: Sequence[Any] = [value]
    elif isinstance(value, (list, tuple)):
        raw_values = value
    else:
        raise ZhiyanReleaseImportError("抖音话题必须是字符串或字符串列表")

    tags: list[str] = []
    seen = set()
    for raw_value in raw_values:
        if not isinstance(raw_value, str):
            raise ZhiyanReleaseImportError("抖音话题列表只能包含字符串")
        for candidate in re.split(r"[#\s,，]+", raw_value.strip()):
            tag = candidate.strip()
            if not tag or tag in seen:
                continue
            tags.append(tag)
            seen.add(tag)

    if len(tags) > MAX_DOUYIN_TAGS:
        raise ZhiyanReleaseImportError(
            f"抖音话题最多 {MAX_DOUYIN_TAGS} 个，当前为 {len(tags)} 个：{', '.join(tags)}"
        )
    if not tags:
        raise ZhiyanReleaseImportError("抖音话题至少需要 1 个")
    return tags


def _assert_same_path(actual: Path, expected: Path, label: str) -> None:
    if actual != expected:
        raise ZhiyanReleaseImportError(f"{label}与发布包顶层素材路径不一致")


def load_zhiyan_release_payload(config_path: Union[str, Path]) -> dict[str, Any]:
    """读取知言发布台 JSON，返回只存草稿的抖音预发布载荷。

    所有媒体路径都必须是已存在的绝对文件路径。该函数无上传、草稿保存或
    正式发布副作用。
    """

    source_path = Path(config_path)
    if not source_path.is_absolute():
        raise ZhiyanReleaseImportError(f"发布台配置必须使用绝对路径：{config_path}")
    if not source_path.exists() or not source_path.is_file():
        raise ZhiyanReleaseImportError(f"发布台配置不存在：{config_path}")

    try:
        config = json.loads(source_path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise ZhiyanReleaseImportError("发布台配置必须使用 UTF-8 编码") from exc
    except json.JSONDecodeError as exc:
        raise ZhiyanReleaseImportError(f"发布台配置 JSON 无效：{exc.msg}") from exc

    if not isinstance(config, Mapping):
        raise ZhiyanReleaseImportError("发布台配置根节点必须是对象")

    _assert_safe_flags(config, "发布包")
    douyin = _find_douyin_platform(config)
    _assert_safe_flags(douyin, "抖音配置")

    topic = _required_text(config, "topic", "选题")
    video_path = _required_absolute_file(config, "video", "视频")
    vertical_cover_path = _required_absolute_file(config, "vertical_cover", "竖版封面")
    horizontal_cover_path = _required_absolute_file(config, "horizontal_cover", "横版封面")

    # 抖音卡片内的素材路径也要独立验证，防止顶层与平台卡片指向不同文件。
    douyin_video_path = _required_absolute_file(douyin, "video", "抖音视频")
    douyin_cover_path = _required_absolute_file(douyin, "cover", "抖音封面")
    _assert_same_path(douyin_video_path, video_path, "抖音视频")
    _assert_same_path(douyin_cover_path, vertical_cover_path, "抖音封面")

    title = _required_text(douyin, "title", "抖音标题", preserve=True)
    detail = _required_text(douyin, "detail", "抖音详情", preserve=True)
    if "#" in detail:
        raise ZhiyanReleaseImportError(
            "抖音详情不能包含手写 # 文本；话题必须从平台候选列表逐个选入"
        )
    tags = _normalize_tags(douyin.get("tags"))

    return {
        "schemaVersion": SCHEMA_VERSION,
        "sourceConfigPath": str(source_path.resolve()),
        "type": DOUYIN_PLATFORM_TYPE,
        "platform": "抖音",
        "topic": topic,
        "videoPath": str(video_path),
        "verticalCoverPath": str(vertical_cover_path),
        "horizontalCoverPath": str(horizontal_cover_path),
        "coverPath": str(vertical_cover_path),
        "coverPaths": {
            "3:4": str(vertical_cover_path),
            "4:3": str(horizontal_cover_path),
        },
        "title": title,
        "detail": detail,
        "tags": tags,
        "debugDryRun": True,
        "saveDraftOnly": True,
        "publishAllowed": False,
    }
