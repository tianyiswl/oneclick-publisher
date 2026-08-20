# -*- coding: utf-8 -*-
"""平台指标同步运行与快照的本地服务。"""

from __future__ import annotations

from datetime import datetime

from . import database
from .platform_data_models import (
    ALLOWED_SOURCE_MODES,
    CollectionBatch,
    CollectionFailure,
    MetricPoint,
)


ALLOWED_ERROR_CODES = frozenset(
    {
        "collector_not_available",
        "session_state_missing",
        "login_required",
        "direct_request_rejected",
        "browser_signature_timeout",
        "browser_cleanup_incomplete",
        "metric_payload_invalid",
        "metric_payload_empty",
        "sync_persist_failed",
    }
)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _account_id(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise CollectionFailure("metric_payload_invalid")
    return value


def _platform_type(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise CollectionFailure("metric_payload_invalid")
    return value


def _source_mode(value: object) -> str:
    if type(value) is not str or value not in ALLOWED_SOURCE_MODES:
        raise CollectionFailure("metric_payload_invalid")
    return value


def _ensure_account(conn, account_id: int, platform_type: int) -> None:
    row = conn.execute(
        "SELECT type FROM user_info WHERE id = ? LIMIT 1",
        (account_id,),
    ).fetchone()
    if row is None or type(row["type"]) is not int or int(row["type"]) != platform_type:
        raise CollectionFailure("metric_payload_invalid")


def _insert_metric_snapshot(
    conn,
    *,
    sync_run_id: int,
    account_id: int,
    platform_type: int,
    point: MetricPoint,
) -> None:
    conn.execute(
        """
        INSERT INTO platform_metric_snapshots
            (syncRunId, accountId, platformType, entityType, entityKey,
             metricKey, rawMetricKey, metricValue, metricUnit, observedAt,
             createdAt)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sync_run_id,
            account_id,
            platform_type,
            point.entity_type,
            point.entity_key,
            point.metric_key,
            point.raw_metric_key,
            point.metric_value,
            point.metric_unit,
            point.observed_at,
            _now(),
        ),
    )


def record_successful_sync(account_id: int, batch: CollectionBatch) -> dict:
    account = _account_id(account_id)
    if type(batch) is not CollectionBatch:
        raise CollectionFailure("metric_payload_invalid")
    platform_type = _platform_type(batch.platform_type)
    source_mode = _source_mode(batch.source_mode)
    finished_at = _now()
    try:
        with database.connect() as conn:
            _ensure_account(conn, account, platform_type)
            cursor = conn.execute(
                """
                INSERT INTO platform_data_sync_runs
                    (accountId, platformType, sourceMode, status, errorCode,
                     metricCount, startedAt, finishedAt)
                VALUES (?, ?, ?, 'success', '', ?, ?, ?)
                """,
                (
                    account,
                    platform_type,
                    source_mode,
                    len(batch.metrics),
                    finished_at,
                    finished_at,
                ),
            )
            sync_run_id = int(cursor.lastrowid)
            for point in batch.metrics:
                _insert_metric_snapshot(
                    conn,
                    sync_run_id=sync_run_id,
                    account_id=account,
                    platform_type=platform_type,
                    point=point,
                )
    except CollectionFailure:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        raise CollectionFailure("sync_persist_failed") from None
    return {
        "accountId": account,
        "status": "success",
        "sourceMode": source_mode,
        "errorCode": "",
        "metricCount": len(batch.metrics),
    }


def record_failed_sync(
    account_id: int,
    platform_type: int,
    source_mode: str,
    error_code: str,
) -> dict:
    account = _account_id(account_id)
    platform = _platform_type(platform_type)
    source = _source_mode(source_mode)
    code = str(error_code or "").strip()
    if code not in ALLOWED_ERROR_CODES:
        code = "metric_payload_invalid"
    finished_at = _now()
    try:
        with database.connect() as conn:
            _ensure_account(conn, account, platform)
            conn.execute(
                """
                INSERT INTO platform_data_sync_runs
                    (accountId, platformType, sourceMode, status, errorCode,
                     metricCount, startedAt, finishedAt)
                VALUES (?, ?, ?, 'failed', ?, 0, ?, ?)
                """,
                (account, platform, source, code, finished_at, finished_at),
            )
    except CollectionFailure:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        raise CollectionFailure("sync_persist_failed") from None
    return {
        "accountId": account,
        "status": "failed",
        "sourceMode": source,
        "errorCode": code,
        "metricCount": 0,
    }


def account_data_summary(account_id: int) -> dict:
    account = _account_id(account_id)
    try:
        with database.connect() as conn:
            latest_run = conn.execute(
                """
                SELECT status, sourceMode, errorCode, metricCount, finishedAt
                FROM platform_data_sync_runs
                WHERE accountId = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (account,),
            ).fetchone()
            latest_success = conn.execute(
                """
                SELECT id, finishedAt
                FROM platform_data_sync_runs
                WHERE accountId = ? AND status = 'success'
                ORDER BY id DESC
                LIMIT 1
                """,
                (account,),
            ).fetchone()
            metrics: dict[str, int | float] = {}
            observed_at: dict[str, str] = {}
            if latest_success is not None:
                rows = conn.execute(
                    """
                    SELECT metricKey, metricValue, observedAt
                    FROM platform_metric_snapshots
                    WHERE syncRunId = ?
                    ORDER BY id
                    """,
                    (int(latest_success["id"]),),
                ).fetchall()
                for row in rows:
                    value = float(row["metricValue"])
                    metrics[str(row["metricKey"])] = (
                        int(value) if value.is_integer() else value
                    )
                    observed_at[str(row["metricKey"])] = str(row["observedAt"])
    except CollectionFailure:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        raise CollectionFailure("sync_persist_failed") from None

    latest_payload = None
    if latest_run is not None:
        latest_payload = {
            "status": str(latest_run["status"]),
            "sourceMode": str(latest_run["sourceMode"]),
            "errorCode": str(latest_run["errorCode"] or ""),
            "metricCount": int(latest_run["metricCount"] or 0),
            "finishedAt": str(latest_run["finishedAt"] or ""),
        }
    return {
        "accountId": account,
        "latestSuccessAt": (
            str(latest_success["finishedAt"]) if latest_success is not None else None
        ),
        "latestRun": latest_payload,
        "metrics": metrics,
        "observedAt": observed_at,
    }
