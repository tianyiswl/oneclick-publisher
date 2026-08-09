# -*- coding: utf-8 -*-
"""离线规划或受控执行抖音带货平台设置采集验证。

默认模式只输出 A/B/C 三个代际的序列，不读取账号、不创建会话。
只有显式传入 ``--execute`` 时才会使用隔离采集协调器；本模块没有
预检、草稿保存、最终提交或任务成功写入接口。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app_core import account_service  # noqa: E402


# 默认规划模式不导入采集器模块，避免提前构造单例和动作队列。
# 测试可在这一公开边界注入仅有允许方法的 fake 协调器。
commerce_collector_manager: Any | None = None

_MIN_ACTION_INTERVAL_SECONDS = 0.8
_SAFETY_STOP_CODES = frozenset(
    {
        "login_required",
        "rate_limited_or_degraded",
        "account_verification_required",
        "verification_required",
        "sms_verification_required",
        "qr_verification_required",
        "challenge_required",
    }
)
_COLLECTOR_KEYS = (
    "domestic_location",
    "favorite_music",
    "local_location",
)
_CLEANUP_RESULTS = frozenset(
    {"", "closed", "not_started", "cleanup_incomplete", "cleanup_unknown"}
)
_ERROR_CODES = frozenset(
    {
        "",
        "login_required",
        "scope_not_confirmed",
        "candidate_panel_missing",
        "candidate_ambiguous",
        "candidate_empty",
        "rate_limited_or_degraded",
        "cleanup_incomplete",
        "collector_start_failed",
        "collector_unknown",
        "stale_result_discarded",
        "account_verification_required",
        "verification_required",
        "sms_verification_required",
        "qr_verification_required",
        "challenge_required",
    }
)
_DIAGNOSTIC_PHASES = frozenset({"result", "queue", "cleanup"})
_DIAGNOSTIC_ACTIONS = frozenset(
    {
        "refresh_favorite_music",
        "search_locations",
        "retry_collector",
        "close_generation",
    }
)
_DIAGNOSTIC_OUTCOMES = frozenset({"success", "failed", "discarded"})
_ACCOUNT_MASK = re.compile(r"account-\d+")
_ABSOLUTE_PATH = re.compile(
    r"(?i)(?:[a-z]:[\\/]|/)(?:[^\s,;|\"'<>]+[\\/]?)+"
)
_UUID = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}\b"
)
_HTML_TAG = re.compile(r"<[^<>]+>")
_COOKIE_KEY_VALUE = re.compile(
    r"(?i)(?:^|[;\s])(?:ttwid|passport_csrf(?:_token)?|"
    r"(?:session|sid|uid|odin|ms)[a-z0-9_-]*|"
    r"[a-z0-9_-]*(?:token|cookie|csrf)[a-z0-9_-]*)\s*="
)
_SENSITIVE_TEXT = re.compile(
    r"(?i)(?:access[_-]?token|cookie|token|html|dom|selector|"
    r"session(?:[_ -]?id)?|verification(?:[_ -]?code)?|验证码|二维码)"
)


class CollectorVerificationError(RuntimeError):
    """验证器固定、不含底层原文的失败。"""

    def __init__(self, code: str) -> None:
        self.code = code if type(code) is str and code else "collector_unknown"
        super().__init__(self.code)


def build_verification_sequences(
    keyword: str,
    domestic_count: int = 20,
) -> list[dict[str, object]]:
    """构造三个固定代际的只读采集顺序。"""

    if type(keyword) is not str or not keyword.strip():
        raise ValueError("验证关键词不能为空")
    if type(domestic_count) is not int or domestic_count < 1:
        raise ValueError("国内搜索次数必须为正整数")
    normalized_keyword = keyword.strip()

    def locations(scope: str, count: int) -> list[dict[str, str]]:
        return [
            {"type": scope, "keyword": normalized_keyword}
            for _ in range(count)
        ]

    return [
        {
            "generation": "A",
            "order": "music-domestic-local",
            "domesticSearchCount": domestic_count,
            "localSearchCount": 2,
            "ending": "normal",
            "actions": [
                {"type": "music", "keyword": ""},
                *locations("domestic", domestic_count),
                *locations("local", 2),
            ],
        },
        {
            "generation": "B",
            "order": "domestic-music-local",
            "domesticSearchCount": domestic_count,
            "localSearchCount": 1,
            "ending": "abandon",
            "actions": [
                *locations("domestic", domestic_count),
                {"type": "music", "keyword": ""},
                *locations("local", 1),
            ],
        },
        {
            "generation": "C",
            "order": "local-domestic-music",
            "domesticSearchCount": 1,
            "localSearchCount": 1,
            "ending": "normal",
            "actions": [
                *locations("local", 1),
                *locations("domestic", 1),
                {"type": "music", "keyword": ""},
            ],
        },
    ]


def _resolve_manager() -> Any:
    global commerce_collector_manager
    if commerce_collector_manager is None:
        from app_core.douyin_commerce_collectors import (  # noqa: PLC0415
            commerce_collector_manager as real_manager,
        )

        commerce_collector_manager = real_manager
    return commerce_collector_manager


def _select_account_file(account_id: int) -> str:
    matches: list[Mapping[str, object]] = []
    for account in account_service.list_accounts():
        if not isinstance(account, Mapping):
            continue
        try:
            matched = bool(
                type(account.get("id")) is int
                and account.get("id") == account_id
                and type(account.get("type")) is int
                and account.get("type") == 3
                and type(account.get("status")) is int
                and account.get("status") == 1
            )
        except Exception:
            continue
        if matched:
            matches.append(account)
    if len(matches) != 1:
        raise CollectorVerificationError("account_not_available")
    try:
        file_path = matches[0].get("filePath")
    except Exception:
        raise CollectorVerificationError("account_not_available") from None
    if type(file_path) is not str or not file_path.strip():
        raise CollectorVerificationError("account_not_available")
    return file_path.strip()


def _short_hash(value: object) -> str:
    if type(value) is not str or not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _safe_short_hash(value: object) -> str:
    return _short_hash(value)


def _safe_report_text(value: object, *, limit: int = 80) -> str:
    if type(value) is not str:
        return ""
    if (
        _ABSOLUTE_PATH.search(value)
        or _UUID.search(value)
        or _HTML_TAG.search(value)
        or _COOKIE_KEY_VALUE.search(value)
        or _SENSITIVE_TEXT.search(value)
    ):
        return "<redacted>"
    return value[:limit]


def _safe_structural_value(
    value: object,
    allowed: frozenset[str],
) -> str:
    return value if type(value) is str and value in allowed else "unknown"


def _safe_cleanup_result(value: object) -> str:
    return value if type(value) is str and value in _CLEANUP_RESULTS else "cleanup_unknown"


def _safe_error_code(value: object) -> str:
    return value if type(value) is str and value in _ERROR_CODES else "collector_unknown"


def _candidate_count(result: object) -> int:
    if not isinstance(result, Mapping):
        return 0
    candidates = result.get("candidates")
    return len(candidates) if type(candidates) is list else 0


def _fixed_error_code(error: object) -> str:
    try:
        code = getattr(error, "code", "")
    except Exception:
        return "collector_unknown"
    return code if type(code) is str and code else "collector_unknown"


def _sanitize_diagnostics(rows: object) -> list[dict[str, object]]:
    if type(rows) is not list:
        return []
    safe_rows: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        collector_type = row.get("collectorType")
        safe_rows.append(
            {
                "timestamp": _safe_report_text(row.get("timestamp"), limit=40),
                "requestIdHash": _safe_short_hash(
                    row.get("requestIdHash") or row.get("requestId")
                ),
                "setupGenerationIdHash": _safe_short_hash(
                    row.get("setupGenerationIdHash")
                    or row.get("setupGenerationId")
                ),
                "collectorType": (
                    collector_type
                    if type(collector_type) is str
                    and collector_type in _COLLECTOR_KEYS
                    else "collector_unknown"
                ),
                "collectorInstanceIdHash": _safe_short_hash(
                    row.get("collectorInstanceIdHash")
                    or row.get("collectorInstanceId")
                ),
                "accountMaskedId": (
                    row.get("accountMaskedId")
                    if type(row.get("accountMaskedId")) is str
                    and _ACCOUNT_MASK.fullmatch(row.get("accountMaskedId"))
                    else "account-redacted"
                ),
                "phase": _safe_structural_value(
                    row.get("phase"), _DIAGNOSTIC_PHASES
                ),
                "action": _safe_structural_value(
                    row.get("action"), _DIAGNOSTIC_ACTIONS
                ),
                "scope": (
                    row.get("scope")
                    if row.get("scope") in {"", "domestic", "local"}
                    else "unknown"
                ),
                "keyword": _safe_report_text(row.get("keyword"), limit=80),
                "attempt": (
                    row.get("attempt") if type(row.get("attempt")) is int else 0
                ),
                "candidateCount": (
                    row.get("candidateCount")
                    if type(row.get("candidateCount")) is int
                    else 0
                ),
                "durationMs": (
                    row.get("durationMs")
                    if type(row.get("durationMs")) is int
                    else 0
                ),
                "outcome": _safe_structural_value(
                    row.get("outcome"), _DIAGNOSTIC_OUTCOMES
                ),
                "errorCode": _safe_error_code(row.get("errorCode")),
                "cleanupResult": _safe_cleanup_result(row.get("cleanupResult")),
            }
        )
    return safe_rows


def _public_close_result(result: object) -> dict[str, object]:
    if not isinstance(result, Mapping):
        return {
            "closed": False,
            "aliveCollectorCount": -1,
            "cleanupResults": {},
        }
    cleanup = result.get("cleanupResults")
    safe_cleanup = (
        {
            key: _safe_cleanup_result(cleanup.get(key))
            for key in _COLLECTOR_KEYS
            if key in cleanup
        }
        if isinstance(cleanup, Mapping)
        else {}
    )
    return {
        "closed": result.get("closed") is True,
        "aliveCollectorCount": (
            result.get("aliveCollectorCount")
            if type(result.get("aliveCollectorCount")) is int
            else -1
        ),
        "cleanupResults": safe_cleanup,
    }


def _instance_hashes(status: object) -> dict[str, str]:
    if not isinstance(status, Mapping):
        return {}
    instances = status.get("collectorInstanceIds")
    if not isinstance(instances, Mapping):
        return {}
    return {
        key: _safe_short_hash(value)
        for key, value in instances.items()
        if type(key) is str and key in _COLLECTOR_KEYS
    }


def _build_probe_source(account_id: int, account_file: str) -> dict[str, object]:
    return {
        "type": 3,
        "workflow": "douyin-commerce",
        "commerceMode": "local-group-buy",
        "contentType": "video",
        "accountId": account_id,
        "accountList": [account_file],
    }


def _write_report(path: Path, report: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            _sanitize_report_for_persistence(report),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _sanitize_report_for_persistence(report: object) -> dict[str, object]:
    """在写盘前重建报告白名单，不信任上游已脱敏结果。"""

    source = report if isinstance(report, Mapping) else {}
    raw_generations = source.get("generations")
    generations: list[dict[str, object]] = []
    if type(raw_generations) is list:
        for raw in raw_generations:
            if not isinstance(raw, Mapping):
                continue
            raw_hashes = raw.get("collectorInstanceHashes")
            hashes = (
                {
                    key: _safe_short_hash(raw_hashes.get(key))
                    for key in _COLLECTOR_KEYS
                    if key in raw_hashes
                }
                if isinstance(raw_hashes, Mapping)
                else {}
            )
            raw_counts = raw.get("candidateCounts")
            counts: dict[str, list[int]] = {}
            if isinstance(raw_counts, Mapping):
                for key in ("favoriteMusic", "domestic", "local"):
                    values = raw_counts.get(key)
                    counts[key] = (
                        [
                            value
                            for value in values
                            if type(value) is int and value >= 0
                        ]
                        if type(values) is list
                        else []
                    )
            generations.append(
                {
                    "generation": (
                        raw.get("generation")
                        if raw.get("generation") in {"A", "B", "C"}
                        else "unknown"
                    ),
                    "order": (
                        raw.get("order")
                        if raw.get("order")
                        in {
                            "music-domestic-local",
                            "domestic-music-local",
                            "local-domestic-music",
                        }
                        else "unknown"
                    ),
                    "ending": (
                        raw.get("ending")
                        if raw.get("ending") in {"normal", "abandon"}
                        else "unknown"
                    ),
                    "outcome": (
                        raw.get("outcome")
                        if raw.get("outcome") in {"completed", "stopped"}
                        else "stopped"
                    ),
                    "stopReason": _safe_error_code(raw.get("stopReason")),
                    "generationIdHash": _safe_short_hash(
                        raw.get("generationIdHash") or raw.get("generationId")
                    ),
                    "collectorInstanceHashes": hashes,
                    "candidateCounts": counts,
                    "diagnostics": _sanitize_diagnostics(raw.get("diagnostics")),
                    "closeResult": _public_close_result(raw.get("closeResult")),
                }
            )
    account_mask = source.get("accountMaskedId")
    interval = source.get("actionIntervalMs")
    return {
        "schemaVersion": "douyin-commerce-collector-verification/v1",
        "accountMaskedId": (
            account_mask
            if type(account_mask) is str and _ACCOUNT_MASK.fullmatch(account_mask)
            else "account-redacted"
        ),
        "actionIntervalMs": (
            max(800, interval) if type(interval) is int else 800
        ),
        "generations": generations,
        "stopReason": _safe_error_code(source.get("stopReason")),
        "finalSubmitCount": 0,
        "draftSaveCount": 0,
        "publicPublishCount": 0,
    }


def _execute_sequences(
    sequences: list[dict[str, object]],
    *,
    account_id: int,
    account_file: str,
) -> tuple[int, dict[str, object]]:
    manager = _resolve_manager()
    report: dict[str, object] = {
        "schemaVersion": "douyin-commerce-collector-verification/v1",
        "accountMaskedId": f"account-{account_id}",
        "actionIntervalMs": int(_MIN_ACTION_INTERVAL_SECONDS * 1000),
        "generations": [],
        "stopReason": "",
        "finalSubmitCount": 0,
        "draftSaveCount": 0,
        "publicPublishCount": 0,
    }
    generation_reports = report["generations"]
    if type(generation_reports) is not list:
        raise CollectorVerificationError("collector_unknown")

    exit_code = 0
    stop_all = False
    for sequence in sequences:
        generation_id = ""
        stop_reason = ""
        candidate_counts = {"favoriteMusic": [], "domestic": [], "local": []}
        status: object = {}
        diagnostics: list[dict[str, object]] = []
        close_result: dict[str, object] = {
            "closed": False,
            "aliveCollectorCount": -1,
            "cleanupResults": {},
        }
        try:
            begun = manager.begin_generation(
                _build_probe_source(account_id, account_file)
            )
            if not isinstance(begun, Mapping):
                raise CollectorVerificationError("collector_start_failed")
            raw_generation_id = begun.get("setupGenerationId")
            if type(raw_generation_id) is not str or not raw_generation_id:
                raise CollectorVerificationError("collector_start_failed")
            generation_id = raw_generation_id

            actions = sequence.get("actions")
            if type(actions) is not list:
                raise CollectorVerificationError("collector_unknown")
            for action in actions:
                if not isinstance(action, Mapping):
                    raise CollectorVerificationError("collector_unknown")
                time.sleep(_MIN_ACTION_INTERVAL_SECONDS)
                action_type = action.get("type")
                if action_type == "music":
                    result = manager.refresh_favorite_music(generation_id)
                    candidate_counts["favoriteMusic"].append(
                        _candidate_count(result)
                    )
                elif action_type in {"domestic", "local"}:
                    result = manager.search_locations(
                        generation_id,
                        action.get("keyword"),
                        action_type,
                    )
                    candidate_counts[action_type].append(_candidate_count(result))
                else:
                    raise CollectorVerificationError("collector_unknown")
                if isinstance(result, Mapping):
                    error_code = result.get("errorCode")
                    if type(error_code) is str and error_code in _SAFETY_STOP_CODES:
                        raise CollectorVerificationError(error_code)
        except Exception as error:
            stop_reason = _fixed_error_code(error)
            if stop_reason == "collector_unknown" and isinstance(
                error, CollectorVerificationError
            ):
                stop_reason = error.code
            stop_all = True
            exit_code = 3 if stop_reason in _SAFETY_STOP_CODES else 4
        finally:
            if generation_id:
                try:
                    status = manager.status(generation_id)
                except Exception:
                    status = {}
            close_reason = (
                "verification_stopped"
                if stop_reason
                else (
                    "verification_abandon"
                    if sequence.get("ending") == "abandon"
                    else "verification_normal"
                )
            )
            try:
                close_result = _public_close_result(
                    manager.close_generation(
                        generation_id or None,
                        reason=close_reason,
                    )
                )
            except Exception:
                close_result = {
                    "closed": False,
                    "aliveCollectorCount": -1,
                    "cleanupResults": {},
                }
            if generation_id:
                try:
                    diagnostics = _sanitize_diagnostics(
                        manager.recent_diagnostics(generation_id, limit=50)
                    )
                except Exception:
                    diagnostics = []
            if (
                close_result.get("closed") is not True
                or close_result.get("aliveCollectorCount") != 0
            ):
                stop_reason = stop_reason or "cleanup_incomplete"
                stop_all = True
                exit_code = exit_code or 4

            generation_reports.append(
                {
                    "generation": sequence.get("generation"),
                    "order": sequence.get("order"),
                    "ending": sequence.get("ending"),
                    "outcome": "stopped" if stop_reason else "completed",
                    "stopReason": stop_reason,
                    "generationIdHash": _short_hash(generation_id),
                    "collectorInstanceHashes": _instance_hashes(status),
                    "candidateCounts": candidate_counts,
                    "diagnostics": diagnostics,
                    "closeResult": close_result,
                }
            )
        if stop_reason:
            report["stopReason"] = stop_reason
        if stop_all:
            break
    return exit_code, report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="抖音带货平台设置隔离采集验证器"
    )
    parser.add_argument("--account-id", type=int, required=True)
    parser.add_argument("--keyword", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        sequences = build_verification_sequences(args.keyword)
    except ValueError as error:
        _parser().error(str(error))

    if not args.execute:
        print(json.dumps(sequences, ensure_ascii=False, indent=2))
        return 0
    if args.output is None:
        print(json.dumps({"ok": False, "errorCode": "output_required"}))
        return 2

    try:
        account_file = _select_account_file(args.account_id)
    except CollectorVerificationError as error:
        print(json.dumps({"ok": False, "errorCode": error.code}))
        return 2

    exit_code, report = _execute_sequences(
        sequences,
        account_id=args.account_id,
        account_file=account_file,
    )
    _write_report(args.output, report)
    print(
        json.dumps(
            {
                "ok": exit_code == 0,
                "output": str(args.output),
                "stopReason": report.get("stopReason", ""),
                "finalSubmitCount": 0,
                "draftSaveCount": 0,
                "publicPublishCount": 0,
            },
            ensure_ascii=False,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
