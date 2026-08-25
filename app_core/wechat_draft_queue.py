# -*- coding: utf-8 -*-
"""硅基进化公众号草稿桥的本地收件箱和安全回执。"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from . import account_service
from .silicon_evolution_draft_package import (
    FrozenWechatDraftPackage,
    FrozenWechatDraftPackageError,
    load_frozen_wechat_draft_package,
)


HANDOFF_SCHEMA = "silicon-evolution-wechat-draft-handoff/v1"
RECEIPT_SCHEMA = "silicon-evolution-wechat-draft-receipt/v1"
TARGET_ACCOUNT = "硅基进化"
HANDOFF_INTENT = "wechat_draft_only"
_ARTICLE_ID = re.compile(r"WX-\d{8}-\d{3}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ERROR_CODE = re.compile(r"错误码\s+([a-z0-9_]+)")
_HANDOFF_KEYS = frozenset(
    {
        "schema_version",
        "article_id",
        "package_path",
        "package_sha256",
        "target_account_name",
        "intent",
        "created_at",
    }
)
_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "article_id",
        "package_sha256",
        "account_name",
        "status",
        "recorded_at",
        "error_code",
    }
)
_STOP_CODES = frozenset(
    {
        "desktop_not_enabled",
        "account_mismatch",
        "login_required",
        "unknown_dialog",
        "save_control_ambiguous",
        "field_readback_mismatch",
        "draft_readback_missing",
        "draft_readback_ambiguous",
    }
)


class WechatDraftQueueError(RuntimeError):
    """收件箱记录不能安全进入公众号草稿任务。"""

    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class DraftQueueResult:
    article_id: str
    package_sha256: str
    status: str
    error_code: str | None
    message: str
    receipt_path: Path | None


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WechatDraftQueueError(
            f"{label}无法读取",
            error_code="field_readback_mismatch",
        ) from exc
    if not isinstance(payload, dict):
        raise WechatDraftQueueError(
            f"{label}必须是对象",
            error_code="field_readback_mismatch",
        )
    return payload


def _require_timezone(value: object) -> None:
    if not isinstance(value, str):
        raise ValueError
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError


def _validate_handoff(payload: dict[str, Any]) -> dict[str, str]:
    try:
        if set(payload) != _HANDOFF_KEYS or payload["schema_version"] != HANDOFF_SCHEMA:
            raise ValueError
        article_id = payload["article_id"]
        package_sha256 = payload["package_sha256"]
        package_path = payload["package_path"]
        if not isinstance(article_id, str) or _ARTICLE_ID.fullmatch(article_id) is None:
            raise ValueError
        if not isinstance(package_sha256, str) or _SHA256.fullmatch(package_sha256) is None:
            raise ValueError
        if not isinstance(package_path, str) or not Path(package_path).is_absolute():
            raise ValueError
        _require_timezone(payload["created_at"])
        if payload["intent"] != HANDOFF_INTENT:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise WechatDraftQueueError(
            "公众号草稿交接记录不符合安全格式",
            error_code="field_readback_mismatch",
        ) from exc
    if payload["target_account_name"] != TARGET_ACCOUNT:
        raise WechatDraftQueueError(
            "公众号草稿交接账号不是硅基进化",
            error_code="account_mismatch",
        )
    return {
        "article_id": article_id,
        "package_path": package_path,
        "package_sha256": package_sha256,
    }


def _atomic_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def _receipt_payload(
    *,
    article_id: str,
    package_sha256: str,
    status: str,
    error_code: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": RECEIPT_SCHEMA,
        "article_id": article_id,
        "package_sha256": package_sha256,
        "account_name": TARGET_ACCOUNT,
        "status": status,
        "recorded_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "error_code": error_code,
    }


def _terminal_receipt_matches(path: Path, article_id: str, package_sha256: str) -> bool:
    if not path.is_file():
        return False
    receipt = _load_json(path, label="既有草稿回执")
    if set(receipt) != _RECEIPT_KEYS:
        return False
    return (
        receipt.get("schema_version") == RECEIPT_SCHEMA
        and receipt.get("article_id") == article_id
        and receipt.get("package_sha256") == package_sha256
        and (
            receipt.get("status") == "draft_readback_confirmed"
            and receipt.get("error_code") is None
            or receipt.get("status") == "stopped"
            and receipt.get("error_code") in _STOP_CODES
        )
    )


def _unique_account(
    target_name: str,
    accounts: list[dict[str, Any]],
) -> dict[str, Any]:
    matches = [
        account
        for account in accounts
        if int(account.get("type") or 0) == 10
        and target_name
        in {
            str(account.get("profileName") or "").strip(),
            str(account.get("userName") or "").strip(),
        }
    ]
    if len(matches) != 1:
        raise WechatDraftQueueError(
            "一键发没有唯一匹配的硅基进化公众号账号",
            error_code="account_mismatch",
        )
    if int(matches[0].get("status") or 0) != 1:
        raise WechatDraftQueueError(
            "硅基进化公众号登录状态未确认",
            error_code="login_required",
        )
    return matches[0]


def _draft_payload(package: FrozenWechatDraftPackage, account: dict[str, Any]) -> dict[str, Any]:
    body_images = [str(path) for path in package.body_image_paths]
    return {
        "type": 10,
        "contentType": "article" if body_images else "text",
        "title": package.title,
        "description": package.content_html,
        "contentHtml": package.content_html,
        "digest": package.digest,
        "coverPath": str(package.cover_path),
        "fileList": body_images,
        "accountIds": [int(account["id"])],
        "accountList": [str(account["filePath"])],
        "runtimeMode": "wechat_draft",
        "debugDryRun": True,
        "wechatGroupNotification": False,
        "enableTimer": False,
        "scheduleTime": None,
        "originalDeclaration": True,
        "backgroundMode": False,
        "wechatArticleTemplate": "silicon-evolution-tech-editorial-v1",
        "articleId": package.article_id,
        "packageSha256": package.package_sha256,
    }


def _default_task_runner(payload: dict[str, Any]) -> dict[str, Any]:
    from . import publish_service, task_service

    task = publish_service.start_desktop_publish([payload])
    deadline = time.monotonic() + 20 * 60
    while time.monotonic() < deadline:
        current = task_service.get_task(int(task["id"]))
        if current and current.get("status") in {"success", "failed", "partial_failed"}:
            items = list(current.get("items") or [])
            item = items[0] if len(items) == 1 else {}
            message = str(item.get("message") or current.get("lastError") or "")
            match = _ERROR_CODE.search(message)
            return {
                "ok": current.get("status") == "success" and item.get("status") == "success",
                "message": message or "公众号草稿任务没有返回可读结果",
                "errorCode": match.group(1) if match else None,
            }
        time.sleep(0.5)
    return {
        "ok": False,
        "message": "公众号草稿任务等待超时",
        "errorCode": "unknown_dialog",
    }


def _process_one(
    handoff_path: Path,
    receipts_dir: Path,
    *,
    account_loader: Callable[[], list[dict[str, Any]]],
    task_runner: Callable[[dict[str, Any]], dict[str, Any]],
) -> DraftQueueResult | None:
    raw = _load_json(handoff_path, label="公众号草稿交接记录")
    article_id = str(raw.get("article_id") or handoff_path.stem)
    package_sha256 = str(raw.get("package_sha256") or "")
    receipt_path = receipts_dir / f"{article_id}.json"
    if _terminal_receipt_matches(receipt_path, article_id, package_sha256):
        return None
    lock_path = receipts_dir / f".{article_id}.{package_sha256}.lock"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return None
    os.close(descriptor)
    try:
        try:
            handoff = _validate_handoff(raw)
            package = load_frozen_wechat_draft_package(
                Path(handoff["package_path"]),
                expected_sha256=handoff["package_sha256"],
            )
            if package.article_id != handoff["article_id"]:
                raise WechatDraftQueueError(
                    "冻结包文章 ID 与交接记录不一致",
                    error_code="field_readback_mismatch",
                )
            account = _unique_account(TARGET_ACCOUNT, account_loader())
            outcome = task_runner(_draft_payload(package, account))
            if outcome.get("ok") is True:
                status = "draft_readback_confirmed"
                error_code = None
            else:
                status = "stopped"
                candidate_code = str(outcome.get("errorCode") or "")
                error_code = candidate_code if candidate_code in _STOP_CODES else "unknown_dialog"
            message = str(outcome.get("message") or "公众号草稿任务已停止")
        except FrozenWechatDraftPackageError as exc:
            status = "stopped"
            error_code = "field_readback_mismatch"
            message = str(exc)
        except WechatDraftQueueError as exc:
            status = "stopped"
            error_code = exc.error_code
            message = str(exc)
        receipt = _receipt_payload(
            article_id=article_id,
            package_sha256=package_sha256,
            status=status,
            error_code=error_code,
        )
        _atomic_json(receipt_path, receipt)
        return DraftQueueResult(
            article_id=article_id,
            package_sha256=package_sha256,
            status=status,
            error_code=error_code,
            message=message,
            receipt_path=receipt_path,
        )
    finally:
        lock_path.unlink(missing_ok=True)


def process_wechat_draft_inbox(
    inbox_dir: Path,
    receipts_dir: Path,
    *,
    enabled: bool,
    account_loader: Callable[[], list[dict[str, Any]]] | None = None,
    task_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> list[DraftQueueResult]:
    """处理当前收件箱；未明确启用时不读取目录也不产生回执。"""
    if enabled is not True:
        return []
    inbox = Path(inbox_dir)
    if not inbox.is_dir():
        return []
    loader = account_loader or account_service.list_accounts
    runner = task_runner or _default_task_runner
    results: list[DraftQueueResult] = []
    for path in sorted(inbox.glob("WX-*.json")):
        result = _process_one(path, Path(receipts_dir), account_loader=loader, task_runner=runner)
        if result is not None:
            results.append(result)
    return results
