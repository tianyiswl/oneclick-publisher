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
_DIAGNOSTIC_FIELDS = (
    "timestamp",
    "requestId",
    "setupGenerationId",
    "collectorType",
    "collectorInstanceId",
    "accountMaskedId",
    "phase",
    "action",
    "scope",
    "keyword",
    "attempt",
    "candidateCount",
    "durationMs",
    "outcome",
    "errorCode",
    "cleanupResult",
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
    matches: list[str] = []
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
            file_path = account.get("filePath")
        except Exception:
            continue
        if matched and type(file_path) is str and file_path.strip():
            matches.append(file_path.strip())
    if len(matches) != 1:
        raise CollectorVerificationError("account_not_available")
    return matches[0]


def _short_hash(value: object) -> str:
    if type(value) is not str or not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


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
        safe: dict[str, object] = {}
        for field in _DIAGNOSTIC_FIELDS:
            value = row.get(field)
            if field in {"setupGenerationId", "collectorInstanceId"}:
                safe[f"{field}Hash"] = _short_hash(value)
            elif type(value) in {type(None), bool, int, float, str}:
                safe[field] = value
        safe_rows.append(safe)
    return safe_rows


def _public_close_result(result: object) -> dict[str, object]:
    if not isinstance(result, Mapping):
        return {
            "closed": False,
            "aliveCollectorCount": -1,
            "cleanupResults": {},
        }
    cleanup = result.get("cleanupResults")
    safe_cleanup = {
        key: value
        for key, value in dict(cleanup).items()
        if type(key) is str and type(value) is str
    } if isinstance(cleanup, Mapping) else {}
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
        key: _short_hash(value)
        for key, value in instances.items()
        if type(key) is str and key in {
            "domestic_location",
            "favorite_music",
            "local_location",
        }
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
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


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
