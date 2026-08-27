# -*- coding: utf-8 -*-
"""抖音图文矩阵的纯数据合同、排期和不可变快照。"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timedelta
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


SCHEMA_VERSION = "oneclick-douyin-graphic-matrix/v1"
WORKFLOW = "douyin-graphic-matrix"
SHANGHAI = ZoneInfo("Asia/Shanghai")
RUNTIME_MODES = frozenset({"local_check", "platform_preflight", "publish"})
MAX_IMAGES = 35
MAX_ACCOUNTS = 20


class DouyinGraphicMatrixError(ValueError):
    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = str(error_code)
        self.public_message = str(message)
        super().__init__(message)


def _invalid(message: str) -> DouyinGraphicMatrixError:
    return DouyinGraphicMatrixError("douyin_graphic_matrix_invalid", message)


def _text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _body(value: Any) -> str:
    return str(value or "").strip()


def _tags(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise _invalid("抖音图文话题必须是列表")
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in value:
        tag = _text(raw).lstrip("#").strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        normalized.append(tag)
    return normalized


def _parse_clock(value: Any, *, field: str) -> time:
    try:
        return datetime.strptime(_text(value), "%H:%M").time()
    except ValueError as exc:
        raise _invalid(f"{field}必须使用 HH:MM 格式") from exc


def _parse_date(value: Any, *, fallback: date) -> date:
    text = _text(value)
    if not text:
        return fallback
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise _invalid("抖音图文起始日期必须使用 YYYY-MM-DD 格式") from exc


def _parse_schedule_time(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise _invalid("抖音图文发布时间必须使用 YYYY-MM-DD HH:MM 格式") from exc
    return parsed.strftime("%Y-%m-%d %H:%M")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise _invalid(f"抖音图文图片不可读取：{path.name}") from exc
    return digest.hexdigest()


def _account_index(accounts: Sequence[Mapping[str, Any]]) -> dict[int, Mapping[str, Any]]:
    indexed: dict[int, Mapping[str, Any]] = {}
    for account in accounts:
        try:
            account_id = int(account.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if account_id > 0:
            indexed[account_id] = account
    return indexed


def _normalized_overrides(raw: Mapping[str, Any]) -> dict[str, Any]:
    title = raw.get("title")
    body = raw.get("body")
    tags = raw.get("tags")
    return {
        "title": None if title is None else _text(title),
        "body": None if body is None else _body(body),
        "tags": None if tags is None else _tags(tags),
    }


def effective_item_payload(
    matrix: Mapping[str, Any], target: Mapping[str, Any]
) -> dict[str, Any]:
    common = dict((matrix.get("content") or {}).get("common") or {})
    overrides = _normalized_overrides(dict(target.get("overrides") or {}))
    title = overrides["title"] if overrides["title"] is not None else _text(common.get("title"))
    body = overrides["body"] if overrides["body"] is not None else _body(common.get("body"))
    tags = overrides["tags"] if overrides["tags"] is not None else _tags(common.get("tags"))
    schedule_time = _parse_schedule_time(target.get("scheduleTime"))
    return {
        "type": 3,
        "contentType": "article",
        "workflow": WORKFLOW,
        "title": title,
        "description": body,
        "tags": list(tags),
        "fileList": list((matrix.get("content") or {}).get("images") or []),
        "accountIds": [int(target.get("accountId") or 0)],
        "enableTimer": bool(schedule_time),
        "scheduleTime": schedule_time or None,
        "scheduleTimezone": "Asia/Shanghai",
        "backgroundMode": False,
    }


def prepare_matrix(
    raw: Mapping[str, Any],
    *,
    accounts: Sequence[Mapping[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise _invalid("抖音图文矩阵任务格式无效")
    if _text(raw.get("schemaVersion")) != SCHEMA_VERSION:
        raise _invalid("抖音图文矩阵 schemaVersion 不受支持")
    if _text(raw.get("workflow")) != WORKFLOW:
        raise _invalid("抖音图文矩阵 workflow 不受支持")
    runtime_mode = _text(raw.get("runtimeMode")) or "local_check"
    if runtime_mode not in RUNTIME_MODES:
        raise _invalid("抖音图文矩阵运行模式无效")

    content_raw = raw.get("content")
    if not isinstance(content_raw, Mapping):
        raise _invalid("抖音图文矩阵缺少内容")
    image_values = content_raw.get("images")
    if not isinstance(image_values, (list, tuple)) or not (1 <= len(image_values) <= MAX_IMAGES):
        raise _invalid("抖音图文图片数量必须为 1 至 35 张")
    images: list[str] = []
    image_hashes: list[str] = []
    for value in image_values:
        path = Path(str(value or "")).expanduser().resolve()
        if not path.is_file():
            raise _invalid(f"抖音图文图片不存在：{path.name or value}")
        images.append(str(path))
        image_hashes.append(_hash_file(path))

    common_raw = content_raw.get("common")
    if not isinstance(common_raw, Mapping):
        raise _invalid("抖音图文缺少通用标题和正文")
    common = {
        "title": _text(common_raw.get("title")),
        "body": _body(common_raw.get("body")),
        "tags": _tags(common_raw.get("tags")),
    }
    if not common["title"] or not common["body"]:
        raise _invalid("抖音图文通用标题和正文不能为空")

    raw_targets = raw.get("targets")
    if not isinstance(raw_targets, (list, tuple)) or not (1 <= len(raw_targets) <= MAX_ACCOUNTS):
        raise _invalid("抖音图文账号数量必须为 1 至 20 个")
    account_rows = _account_index(accounts)
    account_ids: set[int] = set()
    item_indexes: set[int] = set()

    reference = now or datetime.now(SHANGHAI)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=SHANGHAI)
    else:
        reference = reference.astimezone(SHANGHAI)
    schedule_raw = raw.get("schedule") if isinstance(raw.get("schedule"), Mapping) else {}
    start_date = _parse_date(
        schedule_raw.get("startDate"), fallback=(reference + timedelta(days=1)).date()
    )
    start_clock = _parse_clock(schedule_raw.get("startTime") or "18:00", field="抖音图文起始时间")
    try:
        interval_minutes = int(schedule_raw.get("intervalMinutes", 30))
    except (TypeError, ValueError) as exc:
        raise _invalid("抖音图文账号间隔必须是整数分钟") from exc
    if not 1 <= interval_minutes <= 1440:
        raise _invalid("抖音图文账号间隔必须为 1 至 1440 分钟")
    generated_start = datetime.combine(start_date, start_clock, tzinfo=SHANGHAI)

    prepared_targets: list[dict[str, Any]] = []
    matrix_base = {
        "content": {"images": images, "common": common},
    }
    for position, raw_target in enumerate(raw_targets):
        if not isinstance(raw_target, Mapping):
            raise _invalid("抖音图文账号配置格式无效")
        try:
            item_index = int(raw_target.get("itemIndex") or position + 1)
            account_id = int(raw_target.get("accountId") or 0)
        except (TypeError, ValueError) as exc:
            raise _invalid("抖音图文账号或序号无效") from exc
        if item_index <= 0 or item_index in item_indexes:
            raise _invalid("抖音图文任务序号必须唯一")
        if account_id <= 0 or account_id in account_ids:
            raise _invalid("抖音图文账号不能重复")
        account = account_rows.get(account_id)
        if not account or int(account.get("type") or 0) != 3:
            raise _invalid(f"抖音图文账号 {account_id} 不存在或不是抖音账号")
        item_indexes.add(item_index)
        account_ids.add(account_id)
        overrides = _normalized_overrides(dict(raw_target.get("overrides") or {}))
        explicit_schedule = _parse_schedule_time(raw_target.get("scheduleTime"))
        schedule_overridden = bool(raw_target.get("scheduleOverridden") or explicit_schedule)
        schedule_time = explicit_schedule or (
            generated_start + timedelta(minutes=interval_minutes * position)
        ).strftime("%Y-%m-%d %H:%M")
        target = {
            "itemIndex": item_index,
            "accountId": account_id,
            "accountLabel": _text(
                account.get("profileName") or account.get("userName") or f"账号{account_id}"
            ),
            "overrides": overrides,
            "scheduleTime": schedule_time,
            "timezone": "Asia/Shanghai",
            "scheduleOverridden": schedule_overridden,
        }
        target["effective"] = effective_item_payload(matrix_base, target)
        prepared_targets.append(target)

    manifest_path = str(content_raw.get("manifestPath") or "").strip()
    manifest_hash = _text(content_raw.get("manifestSha256"))
    prepared = {
        "schemaVersion": SCHEMA_VERSION,
        "workflow": WORKFLOW,
        "runtimeMode": runtime_mode,
        "content": {
            "manifestPath": manifest_path,
            "manifestSha256": manifest_hash,
            "images": images,
            "imageHashes": image_hashes,
            "common": common,
        },
        "schedule": {
            "startDate": start_date.strftime("%Y-%m-%d"),
            "startTime": start_clock.strftime("%H:%M"),
            "intervalMinutes": interval_minutes,
            "timezone": "Asia/Shanghai",
        },
        "targets": prepared_targets,
    }
    prepared["scopeFingerprint"] = matrix_scope_fingerprint(prepared)
    return prepared


def matrix_scope_fingerprint(matrix: Mapping[str, Any]) -> str:
    content = dict(matrix.get("content") or {})
    canonical = {
        "schemaVersion": SCHEMA_VERSION,
        "workflow": WORKFLOW,
        "manifestSha256": _text(content.get("manifestSha256")),
        "images": list(content.get("imageHashes") or []),
        "targets": [
            {
                "itemIndex": int(target.get("itemIndex") or 0),
                "accountId": int(target.get("accountId") or 0),
                "effective": dict(target.get("effective") or {}),
                "scheduleTime": _text(target.get("scheduleTime")),
                "timezone": "Asia/Shanghai",
            }
            for target in matrix.get("targets") or []
        ],
    }
    encoded = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def retry_matrix(
    matrix: Mapping[str, Any], item_indexes: Sequence[int]
) -> dict[str, Any]:
    wanted = {int(value) for value in item_indexes}
    selected = [
        deepcopy(target)
        for target in matrix.get("targets") or []
        if int(target.get("itemIndex") or 0) in wanted
    ]
    if not selected or {int(row["itemIndex"]) for row in selected} != wanted:
        raise _invalid("抖音图文重试序号不存在")
    retried = deepcopy(dict(matrix))
    retried["runtimeMode"] = "local_check"
    retried["targets"] = selected
    retried["scopeFingerprint"] = matrix_scope_fingerprint(retried)
    return retried
