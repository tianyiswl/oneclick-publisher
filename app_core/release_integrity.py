# -*- coding: utf-8 -*-
"""验证发射台发布清单和升级包完整性。

客户端只保存发布签名公钥。发布私钥必须留在隔离的卖家构建环境中。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

from .branding import ACTIVATION_PRODUCT_ID
from .offline_license import PUBLIC_KEY_E, PUBLIC_KEY_N_HEX


SIGNATURE_PREFIX = "FTR1"
DEFAULT_RELEASE_KEY_ID = "fashetai-release-2026-01"
RELEASE_PUBLIC_KEYS: dict[str, tuple[int, str]] = {
    DEFAULT_RELEASE_KEY_ID: (
        PUBLIC_KEY_E,
        "9da99e57dcce0ef4563fcb2d9f187ce3db3da8fbd79debdabebc77b97c5054c7559206abbf927af989465c6606f9acfbc6a8b912e9465b8d4644dc87e6704f0b651e62dd100d0cf77884064b6cf07b6f9d84fcc8882caeab1bcb65e2ffd906c6efe628cbb9ec0db48c5d88db0b4cb23566a6cd3ae39928f7731b72ac3ca46c9f43e151d13ecd3e0cadb572f2408070dff8f95b67b6daba875036e5fd3a8b9376d5a97b18d0a7c90ecd2ee84ebf2d443b5b371e3c1b5895866f39c932d2e34ac1e519dbbe7eb9bfc55dfc59eb3283f05da95c6de3354c6c3c68260cb87b4420dab6886f4ae269e54c06ab4f82bb766555abb96c3a98169d3f23672ad703d147bc26c4beef4f2abbca268cfd5a0593ba963494c7673715a190da807b25f2b44e8b757b80ba50129b4cf2037d2761410798a4a46e75d3fed0b7234e5efcdd0b4c132d84c929e51ff650801d44d32172447fce7367c98c8a7cbac4298147ac8d96a9c3c9c1caef9be7d20f09e129fadbc4514a38a8f705793860bb7a3a0fbda6b187",
    ),
}


def canonical_manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode((value + padding).encode("ascii"))
    except Exception as exc:
        raise ValueError("发布签名编码不正确。") from exc


def _emsa_pkcs1v15_encode_sha256(message: bytes, size: int) -> bytes:
    digest = hashlib.sha256(message).digest()
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + digest
    padding_length = size - len(digest_info) - 3
    if padding_length < 8:
        raise ValueError("发布签名公钥长度不足。")
    return b"\x00\x01" + (b"\xff" * padding_length) + b"\x00" + digest_info


def verify_manifest_signature(manifest: dict[str, Any], signature_token: str) -> None:
    parts = str(signature_token or "").strip().split(".")
    if len(parts) != 3 or parts[0] != SIGNATURE_PREFIX:
        raise ValueError("不是有效的发射台发布签名。")
    key_id = parts[1]
    key = RELEASE_PUBLIC_KEYS.get(key_id)
    if key is None:
        raise ValueError("发布签名使用了未知密钥。")
    exponent, modulus_hex = key
    modulus = int(modulus_hex, 16)
    size = (modulus.bit_length() + 7) // 8
    signature = _b64url_decode(parts[2])
    if len(signature) != size:
        raise ValueError("发布签名长度无效。")
    signature_number = int.from_bytes(signature, "big")
    if signature_number >= modulus:
        raise ValueError("发布签名无效。")
    actual = pow(signature_number, exponent, modulus).to_bytes(size, "big")
    expected = _emsa_pkcs1v15_encode_sha256(canonical_manifest_bytes(manifest), size)
    if not hmac.compare_digest(actual, expected):
        raise ValueError("发布清单签名无效。")


def verify_release_artifact(
    artifact_path: Path,
    manifest_path: Path,
    signature_path: Path,
) -> dict[str, Any]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("发布清单无法读取。") from exc
    if not isinstance(manifest, dict) or int(manifest.get("schemaVersion") or 0) != 1:
        raise ValueError("发布清单版本不受支持。")
    if manifest.get("productId") != ACTIVATION_PRODUCT_ID:
        raise ValueError("发布包不属于当前产品。")
    if manifest.get("fileName") != artifact_path.name:
        raise ValueError("发布包文件名与清单不一致。")
    verify_manifest_signature(manifest, signature_path.read_text(encoding="ascii"))

    digest = hashlib.sha256()
    with artifact_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if not hmac.compare_digest(digest.hexdigest(), str(manifest.get("sha256") or "")):
        raise ValueError("发布包内容已被篡改。")
    if artifact_path.stat().st_size != int(manifest.get("size") or -1):
        raise ValueError("发布包大小与清单不一致。")
    return manifest
