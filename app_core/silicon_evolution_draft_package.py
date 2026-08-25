# -*- coding: utf-8 -*-
"""硅基进化 V1.2 冻结包的只读导入器。

这里不复用旧内容包导入协议，也不写入公众号。调用者只能得到经过逐文件
校验的标题、摘要、HTML 与本地图片路径，是否保存草稿另由独立执行器决定。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class FrozenWechatDraftPackageError(ValueError):
    """V1.2 冻结包不满足本地草稿导入契约。"""


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REQUIRED_FILES = frozenset({"content.html", "cover-master.png", "publication.json"})


@dataclass(frozen=True)
class FrozenWechatDraftPackage:
    article_id: str
    package_sha256: str
    title: str
    digest: str
    content_html: str
    cover_path: Path
    body_image_paths: tuple[Path, ...]


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FrozenWechatDraftPackageError(f"{label} 无法读取") from exc
    if not isinstance(payload, dict):
        raise FrozenWechatDraftPackageError(f"{label} 必须是对象")
    return payload


def _safe_package_file(root: Path, relative_name: object) -> Path:
    if not isinstance(relative_name, str) or not relative_name:
        raise FrozenWechatDraftPackageError("发布包文件名无效")
    candidate = Path(relative_name)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise FrozenWechatDraftPackageError("发布包包含越界路径")
    resolved = root / candidate
    if resolved.is_symlink() or not resolved.is_file():
        raise FrozenWechatDraftPackageError(f"发布包文件不可安全读取：{relative_name}")
    return resolved


def _verify_file(path: Path, record: object, relative_name: str) -> None:
    if not isinstance(record, dict) or set(record) != {"sha256", "size"}:
        raise FrozenWechatDraftPackageError("发布包文件回执无效")
    expected_sha256 = record["sha256"]
    expected_size = record["size"]
    if (
        not isinstance(expected_sha256, str)
        or _SHA256.fullmatch(expected_sha256) is None
        or type(expected_size) is not int
        or expected_size < 0
    ):
        raise FrozenWechatDraftPackageError("发布包文件回执无效")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise FrozenWechatDraftPackageError(f"发布包文件无法读取：{relative_name}") from exc
    if len(content) != expected_size or hashlib.sha256(content).hexdigest() != expected_sha256:
        raise FrozenWechatDraftPackageError(f"发布包文件哈希不一致：{relative_name}")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FrozenWechatDraftPackageError(f"{label} 不能为空")
    return value.strip()


def load_frozen_wechat_draft_package(
    package_dir: Path,
    *,
    expected_sha256: str,
) -> FrozenWechatDraftPackage:
    """读取一个只允许保存草稿的 V1.2 发布包，逐文件重验哈希。"""
    if not isinstance(expected_sha256, str) or _SHA256.fullmatch(expected_sha256) is None:
        raise FrozenWechatDraftPackageError("package_sha256 必须是 64 位小写十六进制")
    root = Path(package_dir)
    if root.is_symlink() or not root.is_dir():
        raise FrozenWechatDraftPackageError("发布包目录不可安全读取")
    manifest_path = root / "release-manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise FrozenWechatDraftPackageError("发布包缺少 release-manifest.json")
    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError as exc:
        raise FrozenWechatDraftPackageError("release-manifest.json 无法读取") from exc
    if hashlib.sha256(manifest_bytes).hexdigest() != expected_sha256:
        raise FrozenWechatDraftPackageError("发布包哈希不一致")
    manifest = _load_json(manifest_path, "release-manifest.json")
    if manifest.get("state") != "release_ready" or manifest.get("publish_allowed") is not False:
        raise FrozenWechatDraftPackageError("只接受未授权发表的冻结发布包")
    article_id = _text(manifest.get("article_id"), "article_id")
    title = _text(manifest.get("title"), "title")
    files = manifest.get("files")
    if not isinstance(files, dict) or not _REQUIRED_FILES.issubset(files):
        raise FrozenWechatDraftPackageError("发布包缺少草稿所需文件")
    verified: dict[str, Path] = {}
    for relative_name, record in files.items():
        path = _safe_package_file(root, relative_name)
        _verify_file(path, record, relative_name)
        verified[relative_name] = path
    publication = _load_json(verified["publication.json"], "publication.json")
    if publication.get("article_id") != article_id or _text(
        publication.get("title"), "publication.title"
    ) != title:
        raise FrozenWechatDraftPackageError("publication.json 与冻结包文章身份不一致")
    digest = _text(publication.get("digest"), "publication.digest")
    try:
        content_html = verified["content.html"].read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise FrozenWechatDraftPackageError("content.html 无法读取") from exc
    if not content_html.strip():
        raise FrozenWechatDraftPackageError("content.html 不能为空")
    body_image_paths = tuple(
        path for relative_name, path in verified.items() if relative_name.startswith("assets/")
    )
    return FrozenWechatDraftPackage(
        article_id=article_id,
        package_sha256=expected_sha256,
        title=title,
        digest=digest,
        content_html=content_html,
        cover_path=verified["cover-master.png"],
        body_image_paths=body_image_paths,
    )
