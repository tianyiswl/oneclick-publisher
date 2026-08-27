"""抖音图文矩阵最近一次本地填写快照。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from . import database


DRAFT_SCHEMA = "oneclick-douyin-graphic-matrix-draft/v1"
_DRAFT_FILENAME = "douyin_graphic_matrix_draft.json"
_ROOT_KEYS = {
    "schemaVersion",
    "images",
    "accounts",
    "common",
    "schedule",
    "targets",
    "currentStep",
}


class DouyinGraphicMatrixDraftError(ValueError):
    """图文矩阵本地快照无法安全保存或恢复。"""


def _plain_text(value: object) -> str:
    if type(value) is not str:
        raise DouyinGraphicMatrixDraftError("抖音图文矩阵保存内容格式无效")
    return value


def _normalize(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != _ROOT_KEYS:
        raise DouyinGraphicMatrixDraftError("抖音图文矩阵保存内容格式无效")
    if payload.get("schemaVersion") != DRAFT_SCHEMA:
        raise DouyinGraphicMatrixDraftError("抖音图文矩阵保存内容版本不兼容")

    raw_images = payload.get("images")
    if type(raw_images) is not list or len(raw_images) > 35:
        raise DouyinGraphicMatrixDraftError("抖音图文矩阵保存内容格式无效")
    images: list[dict[str, Any]] = []
    for raw in raw_images:
        if not isinstance(raw, Mapping) or set(raw) != {"mediaId", "path"}:
            raise DouyinGraphicMatrixDraftError("抖音图文矩阵保存内容格式无效")
        media_id = raw.get("mediaId")
        path = _plain_text(raw.get("path"))
        if type(media_id) is not int or media_id < 0 or not path.strip():
            raise DouyinGraphicMatrixDraftError("抖音图文矩阵保存内容格式无效")
        images.append({"mediaId": media_id, "path": path})

    raw_accounts = payload.get("accounts")
    if type(raw_accounts) is not list or len(raw_accounts) > 20:
        raise DouyinGraphicMatrixDraftError("抖音图文矩阵保存内容格式无效")
    accounts: list[dict[str, Any]] = []
    for raw in raw_accounts:
        if not isinstance(raw, Mapping) or set(raw) != {"accountId", "filePath"}:
            raise DouyinGraphicMatrixDraftError("抖音图文矩阵保存内容格式无效")
        account_id = raw.get("accountId")
        file_path = _plain_text(raw.get("filePath"))
        if type(account_id) is not int or account_id <= 0:
            raise DouyinGraphicMatrixDraftError("抖音图文矩阵保存内容格式无效")
        accounts.append({"accountId": account_id, "filePath": file_path})

    common = payload.get("common")
    if not isinstance(common, Mapping) or set(common) != {"title", "body", "tags"}:
        raise DouyinGraphicMatrixDraftError("抖音图文矩阵保存内容格式无效")
    tags = common.get("tags")
    if type(tags) is not list or any(type(tag) is not str for tag in tags):
        raise DouyinGraphicMatrixDraftError("抖音图文矩阵保存内容格式无效")
    normalized_common = {
        "title": _plain_text(common.get("title")),
        "body": _plain_text(common.get("body")),
        "tags": list(dict.fromkeys(tag.strip().lstrip("#") for tag in tags if tag.strip().lstrip("#"))),
    }

    schedule = payload.get("schedule")
    if not isinstance(schedule, Mapping) or set(schedule) != {
        "startDate",
        "startTime",
        "intervalMinutes",
    }:
        raise DouyinGraphicMatrixDraftError("抖音图文矩阵保存内容格式无效")
    interval = schedule.get("intervalMinutes")
    if type(interval) is not int or not 1 <= interval <= 1440:
        raise DouyinGraphicMatrixDraftError("抖音图文矩阵保存内容格式无效")
    normalized_schedule = {
        "startDate": _plain_text(schedule.get("startDate")),
        "startTime": _plain_text(schedule.get("startTime")),
        "intervalMinutes": interval,
    }

    targets = payload.get("targets")
    if type(targets) is not list or len(targets) > 20:
        raise DouyinGraphicMatrixDraftError("抖音图文矩阵保存内容格式无效")
    try:
        normalized_targets = json.loads(
            json.dumps(targets, ensure_ascii=False, separators=(",", ":"))
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DouyinGraphicMatrixDraftError("抖音图文矩阵保存内容格式无效") from exc
    if any(
        not isinstance(target, dict)
        or type(target.get("accountId")) is not int
        or target["accountId"] <= 0
        for target in normalized_targets
    ):
        raise DouyinGraphicMatrixDraftError("抖音图文矩阵保存内容格式无效")

    current_step = payload.get("currentStep")
    if type(current_step) is not int or not 0 <= current_step <= 2:
        raise DouyinGraphicMatrixDraftError("抖音图文矩阵保存内容格式无效")
    return {
        "schemaVersion": DRAFT_SCHEMA,
        "images": images,
        "accounts": accounts,
        "common": normalized_common,
        "schedule": normalized_schedule,
        "targets": normalized_targets,
        "currentStep": current_step,
    }


def save_draft(payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _normalize(payload)
    updated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    document = {"payload": normalized, "updatedAt": updated_at}
    serialized = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    target = _draft_path()
    temporary_path: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        temporary_path = Path(raw_path)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
    except Exception as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise DouyinGraphicMatrixDraftError("抖音图文矩阵内容未能写入本机存储") from exc
    return document


def load_draft() -> dict[str, Any] | None:
    target = _draft_path()
    if not target.exists():
        return None
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(document, Mapping) or set(document) != {"payload", "updatedAt"}:
            raise ValueError("invalid document")
        payload = _normalize(document.get("payload"))
        updated_at = document.get("updatedAt")
        if type(updated_at) is not str or not updated_at.strip():
            raise ValueError("invalid timestamp")
    except (TypeError, ValueError, json.JSONDecodeError, DouyinGraphicMatrixDraftError) as exc:
        raise DouyinGraphicMatrixDraftError("抖音图文矩阵已保存内容无法读取") from exc
    except OSError as exc:
        raise DouyinGraphicMatrixDraftError("抖音图文矩阵已保存内容无法读取") from exc
    return {"payload": payload, "updatedAt": updated_at.strip()}


def _draft_path() -> Path:
    return Path(database.DB_PATH).parent / _DRAFT_FILENAME
