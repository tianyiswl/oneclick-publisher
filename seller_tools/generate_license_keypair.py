# -*- coding: utf-8 -*-
"""一次性生成发射台离线授权密钥对。"""

from __future__ import annotations

import argparse
import json
import math
import secrets
from pathlib import Path

try:
    from .license_crypto import DEFAULT_PRIVATE_KEY_FILE
except ImportError:
    from license_crypto import DEFAULT_PRIVATE_KEY_FILE


SMALL_PRIMES = [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37]


def _is_probable_prime(value: int, rounds: int = 40) -> bool:
    if value < 2:
        return False
    for prime in SMALL_PRIMES:
        if value % prime == 0:
            return value == prime
    divisor = value - 1
    power = 0
    while divisor % 2 == 0:
        power += 1
        divisor //= 2
    for _ in range(rounds):
        base = secrets.randbelow(value - 3) + 2
        result = pow(base, divisor, value)
        if result in (1, value - 1):
            continue
        for __ in range(power - 1):
            result = pow(result, 2, value)
            if result == value - 1:
                break
        else:
            return False
    return True


def _generate_prime(bits: int) -> int:
    while True:
        candidate = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        if _is_probable_prime(candidate):
            return candidate


def generate_keypair(bits: int = 2048, exponent: int = 65537) -> tuple[int, int]:
    while True:
        first = _generate_prime(bits // 2)
        second = _generate_prime(bits // 2)
        if first == second:
            continue
        totient = (first - 1) * (second - 1)
        if math.gcd(exponent, totient) == 1:
            modulus = first * second
            private_exponent = pow(exponent, -1, totient)
            return modulus, private_exponent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--private", type=Path, default=DEFAULT_PRIVATE_KEY_FILE)
    parser.add_argument("--public", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if (args.private.exists() or args.public.exists()) and not args.force:
        raise SystemExit("密钥文件已存在；如确认重建，请显式使用 --force。")

    modulus, private_exponent = generate_keypair()
    private_payload = {
        "n_hex": format(modulus, "x"),
        "d_hex": format(private_exponent, "x"),
        "e": 65537,
    }
    public_payload = {"n_hex": format(modulus, "x"), "e": 65537}

    args.private.parent.mkdir(parents=True, exist_ok=True)
    args.public.parent.mkdir(parents=True, exist_ok=True)
    args.private.write_text(json.dumps(private_payload, indent=2), encoding="utf-8")
    args.private.chmod(0o600)
    args.public.write_text(json.dumps(public_payload, indent=2), encoding="utf-8")
    print(f"卖家私钥已生成：{args.private}")
    print(f"客户端公钥已生成：{args.public}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
