# -*- coding: utf-8 -*-
"""抖音带货平台设置预检使用的内置非敏感探针。"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any, Mapping

from conf import RESOURCE_DIR


DOUYIN_COMMERCE_PROBE_RELATIVE_PATH = Path("ui/assets/douyin-commerce-probe.mp4")
_MIN_PROBE_SIZE_BYTES = 1_024
_MAX_PROBE_SIZE_BYTES = 512 * 1024


class DouyinCommerceProbeError(RuntimeError):
    """内置探针资源不可安全使用。"""


def resolve_douyin_commerce_probe(resource_dir: Path | None = None) -> Path:
    """解析并校验只读资源目录中的固定探针视频。"""

    root = Path(resource_dir or RESOURCE_DIR)
    if (
        resource_dir is None
        and getattr(sys, "frozen", False)
        and sys.platform == "darwin"
    ):
        bundle_resources = Path(sys.executable).resolve().parent.parent / "Resources"
        if bundle_resources.is_dir():
            root = bundle_resources
    root = root.resolve()
    probe = (root / DOUYIN_COMMERCE_PROBE_RELATIVE_PATH).resolve()
    if not probe.is_relative_to(root):
        raise DouyinCommerceProbeError("探针路径不在资源目录内")
    if not probe.is_file():
        raise DouyinCommerceProbeError("内置抖音带货探针不存在")

    size = probe.stat().st_size
    if size < _MIN_PROBE_SIZE_BYTES or size > _MAX_PROBE_SIZE_BYTES:
        raise DouyinCommerceProbeError("内置抖音带货探针大小无效")
    with probe.open("rb") as handle:
        if b"ftyp" not in handle.read(32):
            raise DouyinCommerceProbeError("内置抖音带货探针不是 MP4 文件")
    return probe


def build_probe_upload_payload(
    source: Mapping[str, Any], *, resource_dir: Path | None = None
) -> dict[str, Any]:
    """构造只携带账号和工作流身份的预检上传参数。"""

    probe = resolve_douyin_commerce_probe(resource_dir)
    account_id = source.get("accountId")
    if account_id is not None and (
        type(account_id) is not int or account_id < 0
    ):
        raise DouyinCommerceProbeError("探针账号 ID 无效")
    payload = {
        "type": source.get("type", 3),
        "workflow": source.get("workflow", "douyin-commerce"),
        "commerceMode": source.get("commerceMode", "local-group-buy"),
        "contentType": source.get("contentType", "video"),
        "accountList": source.get("accountList", []),
        "fileList": [str(probe)],
        "title": "一键发平台设置探针",
        "description": "仅用于读取平台设置候选，不提交发布",
        "tags": [],
        "runtimeMode": "preflight",
        "debugDryRun": True,
    }
    if account_id is not None:
        payload["accountId"] = account_id
    return payload
