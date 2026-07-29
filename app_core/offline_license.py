# -*- coding: utf-8 -*-
"""验证发射台离线激活码。

客户端只保存公钥。激活码由卖家电脑上的私钥签发，并绑定一台设备。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, time, timezone
from typing import Any

from .branding import ACTIVATION_PRODUCT_ID, LEGACY_ACTIVATION_PRODUCT_IDS


OFFLINE_CODE_PREFIX = "FT1"
PUBLIC_KEY_E = 65537
PUBLIC_KEY_N_HEX = (
    "cf5d33022f901274ae58cd654711703197fdc7975faafdbf2e2cb83b7857bca2"
    "abf3532d7c7d63910de3b6f26019208bb2ac9a0151b1b7f1630642b97f1572ff"
    "3199dca7da7b49f631eaf51f4f027d4912f54184c07b6d91219f993063edbeee"
    "f6fdad267341c3cc773d22da2b109741b98d07a28c520898cda2ceda760a2ae1"
    "3151f2c0fad3083e2282a3463a3c18a6f3595d925bb8578fde81237c508c7511"
    "7e7787860e0b565968b2e82a7e6b35cb0ba701ba40daf0c86c1fbfcc92c644ea"
    "43fe794a66c5f8a8a4e972da6335a76fc1cadcec2e1d2c18f4cee133ec33ecf4"
    "6f16c2c2c6f256b6a722ac72b05da20900044dc37b914889683144e5b757273d"
)


def looks_like_offline_code(code: str) -> bool:
    return code.strip().startswith(f"{OFFLINE_CODE_PREFIX}.")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode((value + padding).encode("ascii"))
    except Exception as exc:
        raise ValueError("激活码编码不正确。") from exc


def _emsa_pkcs1v15_encode_sha256(message: bytes, size: int) -> bytes:
    digest = hashlib.sha256(message).digest()
    digest_info_prefix = bytes.fromhex("3031300d060960864801650304020105000420")
    digest_info = digest_info_prefix + digest
    padding_length = size - len(digest_info) - 3
    if padding_length < 8:
        raise ValueError("授权公钥长度不足。")
    return b"\x00\x01" + (b"\xff" * padding_length) + b"\x00" + digest_info


def _verify_signature(message: bytes, signature: bytes) -> bool:
    if not PUBLIC_KEY_N_HEX:
        raise ValueError("客户端未配置离线授权公钥。")
    modulus = int(PUBLIC_KEY_N_HEX, 16)
    size = (modulus.bit_length() + 7) // 8
    if len(signature) != size:
        return False
    signature_number = int.from_bytes(signature, "big")
    if signature_number >= modulus:
        return False
    actual = pow(signature_number, PUBLIC_KEY_E, modulus).to_bytes(size, "big")
    expected = _emsa_pkcs1v15_encode_sha256(message, size)
    return hmac.compare_digest(actual, expected)


def _parse_expiry(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed_date = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("激活码有效期格式错误。") from exc
    return datetime.combine(parsed_date, time.max, tzinfo=timezone.utc)


def verify_activation_code(
    code: str,
    *,
    expected_machine: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    parts = code.strip().split(".")
    if len(parts) != 3 or parts[0] != OFFLINE_CODE_PREFIX:
        raise ValueError("不是有效的发射台激活码。")

    payload_bytes = _b64url_decode(parts[1])
    signature = _b64url_decode(parts[2])
    if not _verify_signature(payload_bytes, signature):
        raise ValueError("激活码签名无效，请确认激活码完整。")

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception as exc:
        raise ValueError("激活码内容无法读取。") from exc
    if not isinstance(payload, dict) or int(payload.get("v") or 0) != 1:
        raise ValueError("激活码版本不受支持。")

    allowed_products = {ACTIVATION_PRODUCT_ID, *LEGACY_ACTIVATION_PRODUCT_IDS}
    if str(payload.get("p") or "") not in allowed_products:
        raise ValueError("激活码不适用于当前软件。")

    machine = str(payload.get("m") or "").strip().upper()
    if not machine or machine != expected_machine.strip().upper():
        raise ValueError("激活码与当前电脑不匹配。")

    expiry = _parse_expiry(payload.get("x"))
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if expiry and current > expiry:
        raise ValueError("激活码已过有效期。")

    return {
        "activated": True,
        "machineCode": machine,
        "licenseSource": "offline_signed_code",
        "licenseId": str(payload.get("o") or ""),
        "issuedAt": str(payload.get("i") or ""),
        "expiresAt": str(payload.get("x") or ""),
        "edition": str(payload.get("e") or "standard"),
    }
