# -*- coding: utf-8 -*-
"""内容项目统一数据同步策略和项目隔离查询。"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from . import database, platform_data_sync


_BEIJING = ZoneInfo("Asia/Shanghai")
_FAILURE_COOLDOWN = timedelta(minutes=30)
_ACCOUNT_LOCKS: dict[int, threading.Lock] = {}
_ACCOUNT_LOCKS_GUARD = threading.Lock()


class ContentProjectMetricsError(ValueError):
    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = str(error_code)
        self.public_message = str(message)
        super().__init__(message)


def _account_lock(account_id: int) -> threading.Lock:
    with _ACCOUNT_LOCKS_GUARD:
        return _ACCOUNT_LOCKS.setdefault(account_id, threading.Lock())


def _aware(value: object) -> datetime | None:
    if type(value) is not str or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _targets(profile: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_targets = profile.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ContentProjectMetricsError(
            "project_metrics_accounts_unavailable",
            "内容项目没有可同步的平台账号",
        )
    targets: list[dict[str, Any]] = []
    for raw in raw_targets:
        if not isinstance(raw, Mapping):
            raise ContentProjectMetricsError(
                "project_metrics_accounts_unavailable",
                "内容项目平台账号配置无效",
            )
        account_id = raw.get("accountId")
        platform = str(raw.get("platform") or "").strip()
        if type(account_id) is not int or account_id <= 0 or not platform:
            raise ContentProjectMetricsError(
                "project_metrics_accounts_unavailable",
                "内容项目平台账号配置无效",
            )
        targets.append({"accountId": account_id, "platform": platform})
    return targets


class ContentProjectMetricsService:
    """按账号复用现有采集器，不让多个内容项目重复访问平台。"""

    def __init__(
        self,
        *,
        sync_account: Callable[[int], dict[str, Any]] = platform_data_sync.sync_account_data,
        now: Callable[[], datetime] = lambda: datetime.now(_BEIJING),
    ) -> None:
        self.sync_account = sync_account
        self.now = now

    def _current(self) -> datetime:
        current = self.now()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ContentProjectMetricsError(
                "project_metrics_clock_invalid", "项目数据同步时钟必须包含时区"
            )
        return current.astimezone(_BEIJING)

    @staticmethod
    def _run_rows(account_id: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        try:
            with database.connect() as conn:
                latest = conn.execute(
                    """
                    SELECT id, status, sourceMode, errorCode, metricCount,
                           startedAt, finishedAt
                    FROM platform_data_sync_runs
                    WHERE accountId = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (account_id,),
                ).fetchone()
                success = conn.execute(
                    """
                    SELECT id, status, sourceMode, errorCode, metricCount,
                           startedAt, finishedAt
                    FROM platform_data_sync_runs
                    WHERE accountId = ?
                      AND status IN ('success', 'partial_success')
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (account_id,),
                ).fetchone()
                return (
                    dict(latest) if latest is not None else None,
                    dict(success) if success is not None else None,
                )
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            raise ContentProjectMetricsError(
                "sync_persist_failed", "本机同步状态无法读取"
            ) from exc

    @staticmethod
    def _content_count(run_id: int) -> int:
        try:
            with database.connect() as conn:
                row = conn.execute(
                    """
                    SELECT COUNT(DISTINCT entityKey)
                    FROM platform_metric_snapshots
                    WHERE syncRunId = ? AND entityType = 'content'
                    """,
                    (run_id,),
                ).fetchone()
            return int(row[0] or 0) if row is not None else 0
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            raise ContentProjectMetricsError(
                "sync_persist_failed", "本机作品同步数量无法读取"
            ) from exc

    def _policy_row(
        self,
        account_id: int,
        platform: str,
        current: datetime,
    ) -> dict[str, Any]:
        latest, success = self._run_rows(account_id)
        latest_success_at = str((success or {}).get("finishedAt") or "") or None
        success_time = _aware(latest_success_at)
        if success_time is not None and success_time.astimezone(_BEIJING).date() == current.date():
            return {
                "accountId": account_id,
                "platform": platform,
                "action": "reused",
                "status": str(success.get("status") or "success"),
                "errorCode": str(success.get("errorCode") or ""),
                "sourceMode": str(success.get("sourceMode") or ""),
                "metricCount": int(success.get("metricCount") or 0),
                "contentCount": self._content_count(int(success["id"])),
                "latestSuccessAt": latest_success_at,
                "finishedAt": latest_success_at,
            }
        latest_time = _aware((latest or {}).get("finishedAt"))
        if (
            latest is not None
            and str(latest.get("status") or "") == "failed"
            and latest_time is not None
            and current.astimezone(latest_time.tzinfo) - latest_time < _FAILURE_COOLDOWN
        ):
            return {
                "accountId": account_id,
                "platform": platform,
                "action": "cooldown",
                "status": "failed",
                "errorCode": str(latest.get("errorCode") or ""),
                "sourceMode": str(latest.get("sourceMode") or ""),
                "metricCount": int(latest.get("metricCount") or 0),
                "contentCount": 0,
                "latestSuccessAt": latest_success_at,
                "finishedAt": str(latest.get("finishedAt") or "") or None,
            }
        return {
            "accountId": account_id,
            "platform": platform,
            "action": "available",
            "status": str((latest or {}).get("status") or "never_synced"),
            "errorCode": str((latest or {}).get("errorCode") or ""),
            "sourceMode": str((latest or {}).get("sourceMode") or ""),
            "metricCount": int((latest or {}).get("metricCount") or 0),
            "contentCount": 0,
            "latestSuccessAt": latest_success_at,
            "finishedAt": str((latest or {}).get("finishedAt") or "") or None,
        }

    def _sync_target(self, target: Mapping[str, Any], current: datetime) -> dict[str, Any]:
        account_id = int(target["accountId"])
        platform = str(target["platform"])
        with _account_lock(account_id):
            policy = self._policy_row(account_id, platform, current)
            if policy["action"] != "available":
                return policy
            try:
                result = dict(self.sync_account(account_id))
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:
                return {
                    "accountId": account_id,
                    "platform": platform,
                    "action": "synced",
                    "status": "failed",
                    "errorCode": str(
                        getattr(exc, "error_code", "project_metrics_sync_failed")
                    ),
                    "sourceMode": "",
                    "metricCount": 0,
                    "contentCount": 0,
                    "latestSuccessAt": policy["latestSuccessAt"],
                    "finishedAt": self._current().isoformat(timespec="seconds"),
                }
            status = str(result.get("status") or "failed")
            return {
                "accountId": account_id,
                "platform": platform,
                "action": "synced",
                "status": status,
                "errorCode": str(result.get("errorCode") or ""),
                "sourceMode": str(result.get("sourceMode") or ""),
                "metricCount": int(result.get("metricCount") or 0),
                "contentCount": int(result.get("contentCount") or 0),
                "latestSuccessAt": (
                    self._current().isoformat(timespec="seconds")
                    if status in {"success", "partial_success"}
                    else policy["latestSuccessAt"]
                ),
                "finishedAt": self._current().isoformat(timespec="seconds"),
            }

    def sync_project(self, project_id: str, profile: Mapping[str, Any]) -> dict[str, Any]:
        current = self._current()
        started_at = current.isoformat(timespec="seconds")
        accounts = [
            self._sync_target(target, current)
            for target in _targets(profile)
        ]
        return {
            "projectId": str(project_id),
            "startedAt": started_at,
            "finishedAt": self._current().isoformat(timespec="seconds"),
            "accounts": accounts,
        }

    def sync_status(self, project_id: str, profile: Mapping[str, Any]) -> dict[str, Any]:
        current = self._current()
        return {
            "projectId": str(project_id),
            "checkedAt": current.isoformat(timespec="seconds"),
            "accounts": [
                self._policy_row(
                    int(target["accountId"]), str(target["platform"]), current
                )
                for target in _targets(profile)
            ],
        }
