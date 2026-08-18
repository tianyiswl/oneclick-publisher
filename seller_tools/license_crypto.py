# -*- coding: utf-8 -*-
"""卖家端离线授权签名工具的无界面核心。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any


CURRENT_PRIVATE_KEY_FILE = (
    Path.home() / "Documents" / "AI" / "商业授权" / "一键发" / "license-private.json"
)
LEGACY_PRIVATE_KEY_FILE = (
    Path.home() / "Documents" / "AI" / "商业授权" / "发射台" / "license-private.json"
)
DEFAULT_PRIVATE_KEY_FILE = CURRENT_PRIVATE_KEY_FILE


def private_key_file() -> Path:
    override = str(os.getenv("FASHETAI_LICENSE_PRIVATE_KEY") or "").strip()
    if override:
        return Path(override).expanduser()
    if CURRENT_PRIVATE_KEY_FILE.is_file():
        return CURRENT_PRIVATE_KEY_FILE
    if LEGACY_PRIVATE_KEY_FILE.is_file():
        return LEGACY_PRIVATE_KEY_FILE
    return CURRENT_PRIVATE_KEY_FILE


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _canonical_payload_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _emsa_pkcs1v15_encode_sha256(message: bytes, size: int) -> bytes:
    digest = hashlib.sha256(message).digest()
    digest_info_prefix = bytes.fromhex("3031300d060960864801650304020105000420")
    digest_info = digest_info_prefix + digest
    padding_length = size - len(digest_info) - 3
    if padding_length < 8:
        raise ValueError("私钥长度不足。")
    return b"\x00\x01" + (b"\xff" * padding_length) + b"\x00" + digest_info


def _sign(message: bytes, modulus: int, private_exponent: int) -> bytes:
    size = (modulus.bit_length() + 7) // 8
    encoded = _emsa_pkcs1v15_encode_sha256(message, size)
    signature = pow(int.from_bytes(encoded, "big"), private_exponent, modulus)
    return signature.to_bytes(size, "big")


def load_private_key(path: Path | None = None) -> tuple[int, int]:
    target = path or private_key_file()
    if not target.is_file():
        raise FileNotFoundError(f"未找到卖家私钥文件：{target}")
    data = json.loads(target.read_text(encoding="utf-8"))
    return int(str(data["n_hex"]), 16), int(str(data["d_hex"]), 16)


def issue_activation_code(
    machine_code: str,
    *,
    order_number: str = "",
    expires_at: str = "",
    product_id: str | None = None,
    edition: str = "standard",
    key_path: Path | None = None,
) -> str:
    machine = machine_code.strip().upper()
    if re.fullmatch(r"[0-9A-F]{32}", machine) is None:
        raise ValueError("机器码必须是 32 位数字或大写字母 A-F。")
    if expires_at:
        date.fromisoformat(expires_at)

    modulus, private_exponent = load_private_key(key_path)
    if product_id is None:
        from app_core.branding import ACTIVATION_PRODUCT_ID

        product_id = ACTIVATION_PRODUCT_ID
    payload = {
        "e": edition.strip() or "standard",
        "i": date.today().isoformat(),
        "m": machine,
        "o": order_number.strip(),
        "p": product_id,
        "v": 1,
        "x": expires_at,
    }
    payload_bytes = _canonical_payload_bytes(payload)
    signature = _sign(payload_bytes, modulus, private_exponent)
    return f"FT1.{_b64url_encode(payload_bytes)}.{_b64url_encode(signature)}"
