# -*- coding: utf-8 -*-
"""微信公众号内容包读取与安全校验。

内容包只负责把本地标题、正文和封面带入一键发编辑器；不包含账号信息，
不触发上传、保存草稿或最终发表。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "oneclick-wechat-content/v1"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


class WechatContentBundleError(ValueError):
    """公众号内容包不符合安全导入规则。"""


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WechatContentBundleError(f"{label}不能为空")
    return value.strip()


def _bool(value: Any, label: str) -> bool:
    if isinstance(value, bool):
        return value
    raise WechatContentBundleError(f"{label}必须为 true 或 false")


def _relative_file(root: Path, value: Any, label: str) -> Path:
    relative = _text(value, label)
    path = Path(relative)
    if path.is_absolute():
        raise WechatContentBundleError(f"{label}必须使用内容包内的相对路径")
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise WechatContentBundleError(f"{label}不能指向内容包外部文件") from exc
    if not resolved.is_file():
        raise WechatContentBundleError(f"{label}不存在：{relative}")
    return resolved


def load_wechat_content_bundle(manifest_path: str | Path) -> dict[str, str]:
    """读取一个公众号文字内容包，返回可安全带入编辑器的本地内容。"""

    path = Path(manifest_path).expanduser().resolve()
    if not path.is_file():
        raise WechatContentBundleError("请选择内容包中的 manifest.json 文件")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise WechatContentBundleError("manifest.json 必须使用 UTF-8 编码") from exc
    except json.JSONDecodeError as exc:
        raise WechatContentBundleError(f"manifest.json 不是有效 JSON：{exc.msg}") from exc
    if not isinstance(data, Mapping):
        raise WechatContentBundleError("manifest.json 根节点必须是对象")
    if data.get("schemaVersion") != SCHEMA_VERSION:
        raise WechatContentBundleError(f"只支持 {SCHEMA_VERSION} 内容包")
    if data.get("platform") not in {"公众号", "微信公众号"}:
        raise WechatContentBundleError("内容包 platform 必须为 微信公众号")
    if data.get("contentType") != "text":
        raise WechatContentBundleError("当前公众号内容包仅支持 contentType=text")
    if _bool(data.get("debugDryRun"), "debugDryRun") is not True:
        raise WechatContentBundleError("内容包必须保持 debugDryRun=true")
    if _bool(data.get("publishAllowed"), "publishAllowed") is not False:
        raise WechatContentBundleError("内容包必须保持 publishAllowed=false")

    title = _text(data.get("title"), "title")
    body_value = data.get("body")
    body_file_value = data.get("bodyFile")
    if body_value is not None and body_file_value is not None:
        raise WechatContentBundleError("body 与 bodyFile 只能填写一个")
    if body_file_value is not None:
        body_path = _relative_file(path.parent, body_file_value, "bodyFile")
        try:
            body = body_path.read_text(encoding="utf-8").strip()
        except UnicodeDecodeError as exc:
            raise WechatContentBundleError("正文文件必须使用 UTF-8 编码") from exc
    else:
        body = _text(body_value, "body")
    if not body:
        raise WechatContentBundleError("正文不能为空")

    cover = _relative_file(path.parent, data.get("cover"), "cover")
    if cover.suffix.lower() not in IMAGE_SUFFIXES:
        raise WechatContentBundleError("cover 必须是 JPG、PNG 或 WebP 图片")
    return {
        "title": title,
        "body": body,
        "coverPath": str(cover),
        "sourcePath": str(path),
    }
