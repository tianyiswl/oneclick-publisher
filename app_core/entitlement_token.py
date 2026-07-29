# -*- coding: utf-8 -*-
"""验证服务端签发的短期权益令牌。

客户端只包含公钥。服务端私钥、签名实现和数据库均不进入客户包。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any

from .branding import ACTIVATION_PRODUCT_ID, LEGACY_ACTIVATION_PRODUCT_IDS
from .offline_license import PUBLIC_KEY_E, PUBLIC_KEY_N_HEX


TOKEN_PREFIX = "FT2"
DEFAULT_KEY_ID = "fashetai-entitlement-2026-01"
PUBLIC_KEYS: dict[str, tuple[int, str]] = {
    DEFAULT_KEY_ID: (
        PUBLIC_KEY_E,
        "e6e54ca89718c8f9d570bf5eaed3af0379229cb5f58459182d7a07e8388a561fc839946e492c390cf77743b63f0e0f6d1c9a8bec17d6a8b7fd346cb2141cec451e7fc9eddf79697dc4217d064adc7a69b248317430b208263ac02d7a27833270300d48b85ada1e88b88ccc42942383729af9a51c3c3dcbfd9b3d440889b827066e791f9efb6279b8253ff4f36dce7c25e015d97ae03f570a338c7987b3512371c717f4f828e9ef75508483bf5fc48abcb3e1d99cbff791be26e726c0de15d1f55d79c04337235a130b69370175e8740899cb90ea5868cd11902d74231c552b5a9701e632469b7f4997de0a606b82d0dcf8dd33c6b0aa099bbda5c906d1694c60aa351fa99e05f6bdd87f1b95eff6596884161a1938124131684438b72aaadb3fb16f30e39559b4e430c653306e6735ed34477a737f98b23b8abdaa94a69930f4ec59612321fdcdc8a7d4cdcbe79a9814a6dfc28a5e259f75dc3f433221b13a78cf7ebb56a11ec29e93db43be7f246884da5c8f6d0afb2c948c0cecfff7e88d7b",
    ),
}


def looks_like_entitlement_token(value: str) -> bool:
    return str(value or "").strip().startswith(f"{TOKEN_PREFIX}.")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode((value + padding).encode("ascii"))
    except Exception as exc:
        raise ValueError("权益令牌编码不正确。") from exc


def _emsa_pkcs1v15_encode_sha256(message: bytes, size: int) -> bytes:
    digest = hashlib.sha256(message).digest()
    digest_info_prefix = bytes.fromhex("3031300d060960864801650304020105000420")
    digest_info = digest_info_prefix + digest
    padding_length = size - len(digest_info) - 3
    if padding_length < 8:
        raise ValueError("权益公钥长度不足。")
    return b"\x00\x01" + (b"\xff" * padding_length) + b"\x00" + digest_info


def _verify_signature(message: bytes, signature: bytes, key_id: str) -> bool:
    key = PUBLIC_KEYS.get(key_id)
    if key is None:
        raise ValueError("权益令牌使用了未知签名密钥。")
    exponent, modulus_hex = key
    modulus = int(modulus_hex, 16)
    size = (modulus.bit_length() + 7) // 8
    if len(signature) != size:
        return False
    signature_number = int.from_bytes(signature, "big")
    if signature_number >= modulus:
        return False
    actual = pow(signature_number, exponent, modulus).to_bytes(size, "big")
    expected = _emsa_pkcs1v15_encode_sha256(message, size)
    return hmac.compare_digest(actual, expected)


def _parse_time(value: Any, field_name: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"权益令牌缺少 {field_name}。")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"权益令牌 {field_name} 格式错误。") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def verify_entitlement_token(
    token: str,
    *,
    expected_machine: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    parts = str(token or "").strip().split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        raise ValueError("不是有效的发射台权益令牌。")
    payload_bytes = _b64url_decode(parts[1])
    signature = _b64url_decode(parts[2])
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception as exc:
        raise ValueError("权益令牌内容无法读取。") from exc
    if not isinstance(payload, dict):
        raise ValueError("权益令牌内容格式错误。")
    if int(payload.get("v") or 0) != 2 or payload.get("typ") != "entitlement":
        raise ValueError("权益令牌版本或类型不受支持。")
    key_id = str(payload.get("kid") or "")
    if not _verify_signature(payload_bytes, signature, key_id):
        raise ValueError("权益令牌签名无效。")

    allowed_products = {ACTIVATION_PRODUCT_ID, *LEGACY_ACTIVATION_PRODUCT_IDS}
    if str(payload.get("p") or "") not in allowed_products:
        raise ValueError("权益令牌不适用于当前软件。")
    if str(payload.get("status") or "") != "active":
        raise ValueError("权益令牌状态无效。")
    machine = str(payload.get("m") or "").strip().upper()
    if not machine or machine != str(expected_machine or "").strip().upper():
        raise ValueError("权益令牌与当前电脑不匹配。")

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    offline_expiry = _parse_time(payload.get("exp"), "exp")
    if current >= offline_expiry:
        raise ValueError("权益令牌离线宽限已到期。")
    entitlement_expiry_raw = str(payload.get("entitlement_exp") or "").strip()
    if entitlement_expiry_raw:
        entitlement_expiry = _parse_time(entitlement_expiry_raw, "entitlement_exp")
        if current >= entitlement_expiry:
            raise ValueError("授权或试用已到期。")

    features = payload.get("features")
    if not isinstance(features, list) or any(
        not isinstance(feature, str) or not feature.strip() for feature in features
    ):
        raise ValueError("权益令牌功能列表格式错误。")
    normalized_features = tuple(sorted(set(features)))
    plan_id = str(payload.get("plan") or "").strip()
    if not plan_id:
        raise ValueError("权益令牌缺少套餐。")

    return {
        "activated": plan_id != "trial",
        "entitled": True,
        "machineCode": machine,
        "licenseSource": "server_signed_entitlement",
        "licenseId": str(payload.get("lid") or ""),
        "deviceId": str(payload.get("did") or ""),
        "tokenId": str(payload.get("jti") or ""),
        "issuedAt": str(payload.get("iat") or ""),
        "offlineExpiresAt": str(payload.get("exp") or ""),
        "expiresAt": entitlement_expiry_raw,
        "edition": plan_id,
        "planId": plan_id,
        "features": list(normalized_features),
        "keyId": key_id,
    }
