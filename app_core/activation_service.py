# -*- coding: utf-8 -*-
"""桌面端试用和激活服务。

首次启动自动开始七天全功能试用。当前默认接收卖家按本机指纹签发的
离线激活码；部署授权服务后，也可启用 HTTPS 在线权益流程。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import tempfile
import uuid
from datetime import datetime, time, timedelta, timezone
from math import ceil
from pathlib import Path
from typing import Any

import requests

from . import entitlement_token, offline_license
from .branding import ACTIVATION_PRODUCT_ID, APP_TITLE, LEGACY_ACTIVATION_PRODUCT_IDS, TRIAL_DAYS
from .paths import ACTIVATION_FILE, LEGACY_ACTIVATION_FILE


CLOCK_ROLLBACK_TOLERANCE = timedelta(minutes=5)
ENTITLEMENT_REFRESH_WINDOW = timedelta(hours=12)


def _config(name: str, default: Any = "") -> Any:
    try:
        import conf

        return getattr(conf, name, default)
    except Exception:
        return default


def activation_server_url() -> str:
    return str(os.getenv("ACTIVATION_SERVER_URL") or _config("ACTIVATION_SERVER_URL", "") or "").strip()


def activation_timeout() -> int:
    try:
        return int(os.getenv("ACTIVATION_TIMEOUT_SECONDS") or _config("ACTIVATION_TIMEOUT_SECONDS", 15))
    except Exception:
        return 15


def activation_product_id() -> str:
    """允许私有化部署覆盖产品标识，同时默认使用新品牌标识。"""

    return str(
        os.getenv("ACTIVATION_PRODUCT_ID")
        or _config("ACTIVATION_PRODUCT_ID", ACTIVATION_PRODUCT_ID)
        or ACTIVATION_PRODUCT_ID
    ).strip()


def require_online_entitlements() -> bool:
    raw = os.getenv("REQUIRE_ONLINE_ENTITLEMENTS")
    if raw is None:
        raw = _config("REQUIRE_ONLINE_ENTITLEMENTS", False)
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _legacy_machine_code() -> str:
    """计算 0.4.0 早期版本使用的机器码，仅用于兼容已签发授权。"""

    raw = "|".join(
        [
            platform.node(),
            platform.system(),
            platform.release(),
            platform.machine(),
            str(uuid.getnode()),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32].upper()


def _macos_platform_uuid() -> str:
    try:
        result = subprocess.run(
            ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', result.stdout or "")
    return match.group(1).strip().upper() if match else ""


def _windows_machine_guid() -> str:
    try:
        import winreg

        access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            access,
        ) as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
    except (ImportError, OSError):
        return ""
    return str(value or "").strip().upper()


def _linux_machine_id() -> str:
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value.upper()
    return ""


def _stable_machine_identity() -> str:
    """优先使用不会随系统升级、电脑改名或打包环境变化的设备标识。"""

    system = platform.system()
    if system == "Darwin":
        value = _macos_platform_uuid()
        if value:
            return f"macos:{value}"
    elif system == "Windows":
        value = _windows_machine_guid()
        if value:
            return f"windows:{value}"
    elif system == "Linux":
        value = _linux_machine_id()
        if value:
            return f"linux:{value}"

    # 极端精简系统取不到平台 ID 时才降级；不再包含系统版本号。
    return "|".join(
        [
            "fallback",
            platform.node(),
            platform.system(),
            platform.machine(),
            str(uuid.getnode()),
        ]
    )


def machine_code() -> str:
    raw = _stable_machine_identity()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32].upper()


def machine_code_candidates() -> tuple[str, ...]:
    """新版稳定机器码优先，同时兼容在同一台电脑签发的旧版激活码。"""

    return tuple(dict.fromkeys((machine_code(), _legacy_machine_code())))


def _activation_endpoint(base_url: str) -> str:
    url = base_url.rstrip("/")
    if url.endswith("/activate"):
        return url
    return f"{url}/v1/licenses/activate"


def _trial_endpoint(base_url: str) -> str:
    url = base_url.rstrip("/")
    if url.endswith("/trials/start"):
        return url
    return f"{url}/v1/trials/start"


def _refresh_endpoint(base_url: str) -> str:
    url = base_url.rstrip("/")
    if url.endswith("/entitlements/refresh"):
        return url
    return f"{url}/v1/entitlements/refresh"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_datetime(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc).replace(microsecond=0)
    return normalized.isoformat().replace("+00:00", "Z")


def _parse_datetime(value: Any, *, end_of_day: bool = False) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if len(raw) == 10:
        try:
            parsed_date = datetime.strptime(raw, "%Y-%m-%d").date()
            parsed_time = time.max if end_of_day else time.min
            return datetime.combine(parsed_date, parsed_time, tzinfo=timezone.utc)
        except ValueError:
            return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(raw[:19], fmt)
                break
            except ValueError:
                parsed = None
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_json(path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def load_local_license() -> dict[str, Any] | None:
    local = _read_json(ACTIVATION_FILE)
    if local:
        return local
    if ACTIVATION_FILE.name == "license-state.json":
        legacy = _read_json(LEGACY_ACTIVATION_FILE)
        if legacy:
            save_local_license(legacy)
            return legacy
    return None


def save_local_license(data: dict[str, Any]) -> None:
    ACTIVATION_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(data)
    payload.setdefault("machineCode", machine_code())
    if payload.get("activated"):
        payload.setdefault("activatedAt", _now())
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=ACTIVATION_FILE.parent,
        prefix=f".{ACTIVATION_FILE.name}.",
        suffix=".tmp",
    )
    temporary_path = ACTIVATION_FILE.parent / os.path.basename(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temporary_path.chmod(0o600)
        except OSError:
            pass
        temporary_path.replace(ACTIVATION_FILE)
        try:
            ACTIVATION_FILE.chmod(0o600)
        except OSError:
            pass
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _new_trial_state(now: datetime) -> dict[str, Any]:
    return {
        "schemaVersion": 2,
        "activated": False,
        "machineCode": machine_code(),
        "trialStartedAt": _iso_datetime(now),
        "trialEndsAt": _iso_datetime(now + timedelta(days=TRIAL_DAYS)),
        "lastSeenAt": _iso_datetime(now),
    }


def _request_signed_entitlement(
    endpoint: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str = "",
) -> str:
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
    response = requests.post(
        endpoint,
        json=payload,
        headers=headers,
        timeout=activation_timeout(),
        allow_redirects=False,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        try:
            error_data = response.json()
        except Exception:
            error_data = {}
        message = (
            _message(error_data, "")
            if isinstance(error_data, dict)
            else ""
        )
        raise RuntimeError(message or "权益服务请求失败。") from exc
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("权益服务返回格式错误。")
    if not _server_ok(data):
        raise RuntimeError(_message(data, "权益服务拒绝了请求。"))
    nested = data.get("data") if isinstance(data.get("data"), dict) else data
    token = str(
        nested.get("licenseToken")
        or nested.get("license_token")
        or ""
    ).strip()
    if not token:
        raise RuntimeError("权益服务未返回签名许可证。")
    if not entitlement_token.looks_like_entitlement_token(token):
        raise RuntimeError("权益服务返回的不是受支持的服务端权益令牌。")
    return token


def _new_server_trial_state(now: datetime) -> dict[str, Any]:
    server_url = activation_server_url()
    if not server_url.lower().startswith("https://"):
        raise RuntimeError("在线权益服务必须使用 HTTPS。")
    current_machine = machine_code()
    token = _request_signed_entitlement(
        _trial_endpoint(server_url),
        {
            "machineCode": current_machine,
            "productId": activation_product_id(),
        },
    )
    verified = entitlement_token.verify_entitlement_token(
        token,
        expected_machine=current_machine,
        now=now,
    )
    if verified.get("planId") != "trial":
        raise RuntimeError("权益服务返回的不是试用令牌。")
    return {
        "schemaVersion": 3,
        **verified,
        "licenseToken": token,
        "lastSeenAt": _iso_datetime(now),
    }


def _refresh_server_state(
    local: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    server_url = activation_server_url()
    if not server_url.lower().startswith("https://"):
        raise RuntimeError("在线权益服务必须使用 HTTPS。")
    current_machine = machine_code()
    if str(local.get("planId") or "") == "trial":
        return _new_server_trial_state(now)
    current_token = str(local.get("licenseToken") or "").strip()
    token = _request_signed_entitlement(
        _refresh_endpoint(server_url),
        {
            "licenseId": str(local.get("licenseId") or ""),
            "deviceId": str(local.get("deviceId") or ""),
            "machineCode": current_machine,
            "licenseToken": current_token,
            "productId": activation_product_id(),
        },
    )
    verified = entitlement_token.verify_entitlement_token(
        token,
        expected_machine=current_machine,
        now=now,
    )
    return {
        **local,
        **verified,
        "schemaVersion": 3,
        "licenseToken": token,
        "lastSeenAt": _iso_datetime(now),
    }


def _needs_entitlement_refresh(
    verified: dict[str, Any],
    *,
    now: datetime,
) -> bool:
    offline_expiry = _parse_datetime(verified.get("offlineExpiresAt"))
    return bool(
        offline_expiry
        and offline_expiry <= now + ENTITLEMENT_REFRESH_WINDOW
    )


def _load_or_create_state(now: datetime) -> dict[str, Any]:
    local = load_local_license()
    if local:
        return dict(local)
    if require_online_entitlements():
        try:
            state = _new_server_trial_state(now)
        except Exception as exc:
            return {
                "schemaVersion": 3,
                "activated": False,
                "machineCode": machine_code(),
                "initializationError": str(exc) or "权益服务暂时不可用",
                "lastSeenAt": _iso_datetime(now),
            }
    else:
        state = _new_trial_state(now)
    save_local_license(state)
    return state


def _server_ok(data: dict[str, Any]) -> bool:
    if data.get("ok") is True or data.get("success") is True:
        return True
    if data.get("code") in (0, 200, "0", "200"):
        return True
    status = str(data.get("status") or "").lower()
    return status in {"active", "activated", "success", "ok"}


def _expires_text(data: dict[str, Any]) -> str:
    for key in ("expiresAt", "expireAt", "expiredAt", "validUntil", "endAt"):
        value = data.get(key)
        if value:
            return str(value)
    nested = data.get("data")
    if isinstance(nested, dict):
        return _expires_text(nested)
    return ""


def _message(data: dict[str, Any], default: str) -> str:
    for key in ("message", "msg", "error"):
        value = data.get(key)
        if value:
            return str(value)
    nested = data.get("data")
    if isinstance(nested, dict):
        return _message(nested, default)
    return default


def _verified_entitlement(
    local: dict[str, Any],
    *,
    expected_machine: str,
    now: datetime,
) -> tuple[dict[str, Any] | None, str]:
    """验证持久化授权，普通 JSON 字段不能独立产生有效权益。"""

    token = str(local.get("licenseToken") or "").strip()
    if not token:
        if bool(local.get("activated")):
            return None, "授权状态缺少签名许可证"
        return None, ""
    errors: list[str] = []
    candidates = tuple(dict.fromkeys((expected_machine, *machine_code_candidates())))
    for candidate in candidates:
        try:
            if entitlement_token.looks_like_entitlement_token(token):
                verified = entitlement_token.verify_entitlement_token(
                    token,
                    expected_machine=candidate,
                    now=now,
                )
            else:
                verified = offline_license.verify_activation_code(
                    token,
                    expected_machine=candidate,
                    now=now,
                )
        except (TypeError, ValueError) as exc:
            errors.append(str(exc) or "签名许可证验证失败")
            continue
        licensed_machine = str(verified.get("machineCode") or candidate).strip().upper()
        return {
            **verified,
            "licensedMachineCode": licensed_machine,
            "machineCode": expected_machine,
        }, ""
    return None, (errors[0] if errors else "签名许可证验证失败")


def license_status(*, now: datetime | None = None) -> dict[str, Any]:
    current = (now or _utc_now()).astimezone(timezone.utc)
    local = _load_or_create_state(current)
    current_machine_code = machine_code()
    verified_entitlement, integrity_error = _verified_entitlement(
        local,
        expected_machine=current_machine_code,
        now=current,
    )
    refresh_error = ""
    if (
        require_online_entitlements()
        and not verified_entitlement
        and str(local.get("planId") or "") == "trial"
        and "离线宽限已到期" in integrity_error
    ):
        try:
            local = _refresh_server_state(local, now=current)
            verified_entitlement, integrity_error = _verified_entitlement(
                local,
                expected_machine=current_machine_code,
                now=current,
            )
        except Exception as exc:
            refresh_error = str(exc) or "试用权益刷新失败"
    if (
        require_online_entitlements()
        and verified_entitlement
        and entitlement_token.looks_like_entitlement_token(
            str(local.get("licenseToken") or "")
        )
        and _needs_entitlement_refresh(verified_entitlement, now=current)
    ):
        try:
            local = _refresh_server_state(local, now=current)
            verified_entitlement, integrity_error = _verified_entitlement(
                local,
                expected_machine=current_machine_code,
                now=current,
            )
        except Exception as exc:
            # 当前签名令牌仍在宽限内时允许离线继续，但向界面暴露刷新失败。
            refresh_error = str(exc) or "权益刷新失败"
    if verified_entitlement:
        local.update(verified_entitlement)
    stored_machine_code = str(local.get("machineCode") or "").strip().upper()
    token = str(local.get("licenseToken") or "").strip()
    unsigned_trial_state = not token and not bool(local.get("activated"))
    if unsigned_trial_state and stored_machine_code != current_machine_code:
        # 早期机器码会受系统版本、电脑名或打包环境影响。
        # 试用状态仅迁移设备码，不修改原开始日期和到期日期。
        local["machineCode"] = current_machine_code
        stored_machine_code = current_machine_code
    machine_matches = bool(verified_entitlement) or not stored_machine_code or (
        stored_machine_code == current_machine_code
    )
    last_seen = _parse_datetime(local.get("lastSeenAt"))
    clock_rollback = bool(last_seen and current < last_seen - CLOCK_ROLLBACK_TOLERANCE)

    entitled = bool(verified_entitlement and verified_entitlement.get("entitled", True))
    activated = bool(verified_entitlement and verified_entitlement.get("activated", True))
    initialization_error = str(local.get("initializationError") or "").strip()
    expires_at = _expires_text(local)
    expiry = _parse_datetime(expires_at, end_of_day=True)
    activation_expired = bool(activated and expiry and current > expiry)

    trial_started = _parse_datetime(local.get("trialStartedAt"))
    trial_ends = _parse_datetime(local.get("trialEndsAt"))
    if not trial_started or not trial_ends:
        trial_started = current
        trial_ends = current + timedelta(days=TRIAL_DAYS)
        local["trialStartedAt"] = _iso_datetime(trial_started)
        local["trialEndsAt"] = _iso_datetime(trial_ends)

    remaining_seconds = max(0, int((trial_ends - current).total_seconds()))
    remaining_days = ceil(remaining_seconds / 86400) if remaining_seconds else 0
    signed_trial = bool(
        entitled
        and verified_entitlement
        and verified_entitlement.get("planId") == "trial"
    )
    if signed_trial and expiry:
        trial_ends = expiry
        remaining_seconds = max(0, int((trial_ends - current).total_seconds()))
        remaining_days = ceil(remaining_seconds / 86400) if remaining_seconds else 0
    trial_active = signed_trial or (not entitled and current < trial_ends)

    if initialization_error:
        mode = "entitlement_service_error"
        status_text = "无法验证试用或授权"
        color = "#dc2626"
        access_allowed = False
    elif integrity_error:
        mode = "license_integrity_error"
        status_text = "授权签名无效"
        color = "#dc2626"
        access_allowed = False
    elif clock_rollback:
        mode = "clock_error"
        status_text = "系统时间异常"
        color = "#dc2626"
        access_allowed = False
    elif not machine_matches:
        mode = "machine_mismatch"
        status_text = "授权设备不匹配"
        color = "#dc2626"
        access_allowed = False
    elif activated and not activation_expired:
        mode = "activated"
        status_text = "已激活"
        color = "#059669"
        access_allowed = True
    elif activation_expired:
        mode = "license_expired"
        status_text = "授权已到期"
        color = "#dc2626"
        access_allowed = False
    elif trial_active:
        mode = "trial"
        status_text = f"试用中（剩余 {remaining_days} 天）"
        color = "#d97706"
        access_allowed = True
    else:
        mode = "trial_expired"
        status_text = "7 天试用已到期"
        color = "#dc2626"
        access_allowed = False

    if refresh_error and access_allowed:
        status_text = f"{status_text}（离线使用，待联网复核）"
        color = "#d97706"

    if not clock_rollback and not initialization_error:
        if unsigned_trial_state or verified_entitlement:
            local["machineCode"] = current_machine_code
        else:
            local["machineCode"] = stored_machine_code or current_machine_code
        local["lastSeenAt"] = _iso_datetime(current)
        save_local_license(local)

    return {
        "activated": activated,
        "entitled": entitled,
        "accessAllowed": access_allowed,
        "mode": mode,
        "statusText": status_text,
        "color": color,
        # 授权窗口必须显示当前真实机器码，不得回显旧状态中的过期机器码。
        "machineCode": current_machine_code,
        "licensedMachineCode": str(local.get("licensedMachineCode") or ""),
        "serverUrl": activation_server_url(),
        "expiresAt": expires_at,
        "trialStartedAt": _iso_datetime(trial_started),
        "trialEndsAt": _iso_datetime(trial_ends),
        "trialRemainingDays": remaining_days,
        "clockRollbackDetected": clock_rollback,
        "licenseIntegrityError": integrity_error,
        "initializationError": initialization_error,
        "refreshError": refresh_error,
        "offlineExpiresAt": (
            verified_entitlement.get("offlineExpiresAt", "")
            if verified_entitlement
            else ""
        ),
        "planId": (
            verified_entitlement.get("planId", "")
            if verified_entitlement
            else ""
        ),
        "features": (
            list(verified_entitlement.get("features") or [])
            if verified_entitlement
            else []
        ),
        "activatedAt": local.get("activatedAt", ""),
    }


def activate(code: str) -> dict[str, Any]:
    activation_code = code.strip()
    if not activation_code:
        raise ValueError("请输入激活码。")
    if offline_license.looks_like_offline_code(activation_code):
        current_machine_code = machine_code()
        license_payload = None
        errors: list[str] = []
        for candidate in machine_code_candidates():
            try:
                verified = offline_license.verify_activation_code(
                    activation_code,
                    expected_machine=candidate,
                )
            except ValueError as exc:
                errors.append(str(exc))
                continue
            license_payload = {
                **verified,
                "licensedMachineCode": str(verified.get("machineCode") or candidate),
                "machineCode": current_machine_code,
            }
            break
        if license_payload is None:
            raise ValueError(errors[0] if errors else "激活码验证失败。")
        existing = load_local_license() or {}
        saved = {
            **existing,
            **license_payload,
            "licenseToken": activation_code,
            "activationCodeMasked": mask_code(activation_code),
            "activatedAt": _now(),
        }
        save_local_license(saved)
        return license_status()

    server_url = activation_server_url()
    if not server_url:
        raise RuntimeError("激活码格式不正确，且当前未配置在线激活服务。")
    if not server_url.lower().startswith("https://"):
        raise RuntimeError("在线激活服务必须使用 HTTPS，已拒绝不安全的 HTTP 地址。")

    current_machine_code = machine_code()
    product_id = activation_product_id()
    payload = {
        "activationCode": activation_code,
        "activation_code": activation_code,
        "machineCode": current_machine_code,
        "machine_code": current_machine_code,
        "hardwareId": current_machine_code,
        "app": product_id,
        "productId": product_id,
        "appName": APP_TITLE,
        "legacyAppIds": list(LEGACY_ACTIVATION_PRODUCT_IDS),
    }
    license_token = _request_signed_entitlement(
        _activation_endpoint(server_url),
        payload,
        idempotency_key=str(uuid.uuid4()),
    )
    verified_payload = entitlement_token.verify_entitlement_token(
        license_token,
        expected_machine=current_machine_code,
    )
    existing = load_local_license() or {}
    saved = {
        **existing,
        **verified_payload,
        "licenseToken": license_token,
        "activationCodeMasked": mask_code(activation_code),
        "machineCode": current_machine_code,
        "serverUrl": server_url,
        "activatedAt": _now(),
    }
    save_local_license(saved)
    return license_status()


def mask_code(code: str) -> str:
    cleaned = code.strip()
    if len(cleaned) <= 8:
        return "*" * len(cleaned)
    return f"{cleaned[:4]}****{cleaned[-4:]}"
