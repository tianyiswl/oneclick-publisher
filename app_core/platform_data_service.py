# -*- coding: utf-8 -*-
"""平台指标同步运行与快照的本地服务。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

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
        "account_trends_unavailable",
        "content_list_unavailable",
        "content_list_truncated",
        "content_payload_invalid",
    }
)
ALLOWED_SYNC_STATUSES = frozenset({"success", "partial_success", "failed"})

_PERIOD_DAYS = frozenset({1, 7, 30})
_BATCH_WARNING_CODES = frozenset(
    {
        "account_trends_unavailable",
        "content_list_unavailable",
        "content_list_truncated",
        "content_payload_invalid",
    }
)
_ACCOUNT_DAILY_METRICS = (
    "views",
    "likes",
    "comments",
    "shares",
    "followers_net",
    "profile_visits",
)
_ACCOUNT_LIFETIME_METRICS = ("followers_total",)
_PUBLIC_CONTENT_METRICS = frozenset(
    {"views", "likes", "comments", "shares", "favorites", "downloads"}
)
_PUBLIC_URL_ROOT_DOMAINS = (
    "creator.douyin.com",
    "douyin.com",
    "douyinpic.com",
    "douyincdn.com",
)
_BEIJING = ZoneInfo("Asia/Shanghai")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _beijing_today() -> date:
    return datetime.now(_BEIJING).date()


def _built_in_int(
    value: object,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if type(value) is not int or value < minimum:
        raise CollectionFailure("metric_payload_invalid")
    if maximum is not None and value > maximum:
        raise CollectionFailure("metric_payload_invalid")
    return value


def _period_range(days: object) -> tuple[int, str, str]:
    if type(days) is not int or days not in _PERIOD_DAYS:
        raise CollectionFailure("metric_payload_invalid")
    end = _beijing_today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    return days, start.isoformat(), end.isoformat()


def _public_number(value: object) -> int | float:
    number = float(value)
    return int(number) if number.is_integer() else number


def _public_url(value: object) -> str:
    if type(value) is not str:
        return ""
    parsed = urlsplit(value)
    hostname = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or not any(
            hostname == root or hostname.endswith(f".{root}")
            for root in _PUBLIC_URL_ROOT_DOMAINS
        )
    ):
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


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
             metricKey, rawMetricKey, metricValue, metricUnit, metricScope,
             periodStart, periodEnd, observedAt, createdAt)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            point.metric_scope,
            point.period_start,
            point.period_end,
            point.observed_at,
            _now(),
        ),
    )


def _upsert_content(
    conn,
    *,
    account_id: int,
    platform_type: int,
    content,
    seen_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO platform_contents
            (accountId, platformType, contentId, title, coverUrl, publishedAt,
             contentStatus, contentType, firstSeenAt, lastSeenAt)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(accountId, platformType, contentId) DO UPDATE SET
            title = excluded.title,
            coverUrl = excluded.coverUrl,
            publishedAt = excluded.publishedAt,
            contentStatus = excluded.contentStatus,
            contentType = excluded.contentType,
            lastSeenAt = excluded.lastSeenAt
        """,
        (
            account_id,
            platform_type,
            content.content_id,
            content.title,
            _public_url(content.cover_url),
            content.published_at,
            content.content_status,
            content.content_type,
            seen_at,
            seen_at,
        ),
    )


def _finalize_sync_run(
    conn,
    *,
    sync_run_id: int,
    status: str,
    error_code: str,
    metric_count: int,
    finished_at: str,
) -> None:
    if status not in ALLOWED_SYNC_STATUSES:
        raise CollectionFailure("metric_payload_invalid")
    cursor = conn.execute(
        """
        UPDATE platform_data_sync_runs
        SET status = ?, errorCode = ?, metricCount = ?, finishedAt = ?
        WHERE id = ? AND status = 'in_progress'
        """,
        (status, error_code, metric_count, finished_at, sync_run_id),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("sync run finalization failed")


def _warning_code(value: object) -> str:
    if type(value) is not str or value != value.strip():
        raise CollectionFailure("metric_payload_invalid")
    if value and value not in _BATCH_WARNING_CODES:
        raise CollectionFailure("metric_payload_invalid")
    return value


def _validate_metric_identities(
    account_id: int,
    batch: CollectionBatch,
) -> None:
    account_entity_key = f"account:{account_id}"
    content_ids = {content.content_id for content in batch.contents}
    for point in batch.metrics:
        if point.entity_type == "account" and point.entity_key != account_entity_key:
            raise CollectionFailure("metric_payload_invalid")
        if point.entity_type == "content" and point.entity_key not in content_ids:
            raise CollectionFailure("metric_payload_invalid")


def _record_collection_sync(
    account_id: int,
    batch: CollectionBatch,
) -> dict:
    account = _account_id(account_id)
    if type(batch) is not CollectionBatch:
        raise CollectionFailure("metric_payload_invalid")
    platform_type = _platform_type(batch.platform_type)
    source_mode = _source_mode(batch.source_mode)
    warning_code = _warning_code(batch.warning_code)
    _validate_metric_identities(account, batch)
    complete = (
        batch.account_metrics_available
        and batch.content_data_available
        and not warning_code
    )
    status = "success" if complete else "partial_success"
    error_code = "" if complete else warning_code
    finished_at = _now()
    try:
        with database.connect() as conn:
            _ensure_account(conn, account, platform_type)
            cursor = conn.execute(
                """
                INSERT INTO platform_data_sync_runs
                    (accountId, platformType, sourceMode, status, errorCode,
                     metricCount, startedAt, finishedAt)
                VALUES (?, ?, ?, 'in_progress', '', 0, ?, NULL)
                """,
                (
                    account,
                    platform_type,
                    source_mode,
                    batch.platform_observed_at,
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
            for content in batch.contents:
                _upsert_content(
                    conn,
                    account_id=account,
                    platform_type=platform_type,
                    content=content,
                    seen_at=finished_at,
                )
            _finalize_sync_run(
                conn,
                sync_run_id=sync_run_id,
                status=status,
                error_code=error_code,
                metric_count=len(batch.metrics),
                finished_at=finished_at,
            )
    except CollectionFailure:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        raise CollectionFailure("sync_persist_failed") from None
    return {
        "accountId": account,
        "status": status,
        "sourceMode": source_mode,
        "errorCode": error_code,
        "metricCount": len(batch.metrics),
    }


def record_collection_sync(account_id: int, batch: CollectionBatch) -> dict:
    return _record_collection_sync(account_id, batch)


def record_successful_sync(account_id: int, batch: CollectionBatch) -> dict:
    """兼容旧同步编排签名，并保留 V2 批次的真实完整度。"""

    return _record_collection_sync(account_id, batch)


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


def _latest_run_details(
    conn,
    account_id: int,
) -> dict | None:
    latest_attempt = conn.execute(
        """
        SELECT status, sourceMode, errorCode, metricCount, finishedAt
        FROM platform_data_sync_runs
        WHERE accountId = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (account_id,),
    ).fetchone()
    if latest_attempt is None:
        return None
    return {
        "status": str(latest_attempt["status"]),
        "sourceMode": str(latest_attempt["sourceMode"]),
        "errorCode": str(latest_attempt["errorCode"] or ""),
        "metricCount": int(latest_attempt["metricCount"] or 0),
        "finishedAt": str(latest_attempt["finishedAt"] or ""),
    }


def _content_query_state(conn, account_id: int) -> dict:
    run_projection = """
        SELECT runs.id, runs.status, runs.errorCode,
               runs.startedAt, runs.finishedAt,
               EXISTS (
                   SELECT 1
                   FROM platform_metric_snapshots AS snapshots
                   WHERE snapshots.syncRunId = runs.id
                     AND snapshots.entityType = 'content'
                     AND snapshots.metricScope = 'lifetime_total'
                     AND snapshots.periodStart != ''
                     AND snapshots.periodEnd != ''
               ) AS hasContentMetrics
        FROM platform_data_sync_runs AS runs
    """
    latest_attempt = conn.execute(
        run_projection
        + """
        WHERE runs.accountId = ?
        ORDER BY runs.id DESC
        LIMIT 1
        """,
        (account_id,),
    ).fetchone()
    latest_trusted = conn.execute(
        run_projection
        + """
        WHERE runs.accountId = ?
          AND runs.status IN ('success', 'partial_success')
          AND runs.errorCode IN ('', 'content_list_truncated',
                                 'account_trends_unavailable')
          AND EXISTS (
              SELECT 1
              FROM platform_metric_snapshots AS snapshots
              WHERE snapshots.syncRunId = runs.id
                AND snapshots.entityType = 'content'
                AND snapshots.metricScope = 'lifetime_total'
                AND snapshots.periodStart != ''
                AND snapshots.periodEnd != ''
          )
        ORDER BY runs.id DESC
        LIMIT 1
        """,
        (account_id,),
    ).fetchone()

    availability = "missing"
    warning_code = ""
    if latest_attempt is not None:
        status = str(latest_attempt["status"] or "")
        error_code = str(latest_attempt["errorCode"] or "")
        has_content_metrics = bool(latest_attempt["hasContentMetrics"])
        if error_code in {"content_list_unavailable", "content_payload_invalid"}:
            availability = "unavailable"
            warning_code = error_code
        elif error_code == "content_list_truncated":
            if has_content_metrics:
                availability = "partial"
                warning_code = error_code
            else:
                availability = "unavailable"
                warning_code = "content_payload_invalid"
        elif (
            status in {"success", "partial_success"}
            and error_code in {"", "account_trends_unavailable"}
            and has_content_metrics
        ):
            availability = "available"

    data_run = (
        latest_attempt
        if availability in {"available", "partial"}
        else latest_trusted
    )
    data_rows = [data_run] if data_run is not None else []
    return {
        "availability": availability,
        "warningCode": warning_code,
        "platformObservedAt": _latest_controlled_timestamp(
            data_rows, "startedAt"
        ),
        "localSyncedAt": _latest_controlled_timestamp(
            data_rows, "finishedAt"
        ),
    }


def _latest_account_rows(
    conn,
    *,
    account_id: int,
    period_start: str,
    period_end: str,
    scope: str,
):
    return conn.execute(
        """
        WITH ranked AS (
            SELECT snapshots.metricKey, snapshots.metricValue,
                   snapshots.metricUnit, snapshots.metricScope,
                   snapshots.periodStart, snapshots.periodEnd,
                   snapshots.observedAt, runs.sourceMode, runs.finishedAt,
                   ROW_NUMBER() OVER (
                       PARTITION BY snapshots.metricKey,
                                    snapshots.metricScope,
                                    snapshots.periodStart,
                                    snapshots.periodEnd
                       ORDER BY snapshots.id DESC
                   ) AS rowNumber
            FROM platform_metric_snapshots AS snapshots
            JOIN platform_data_sync_runs AS runs
              ON runs.id = snapshots.syncRunId
            WHERE snapshots.accountId = ?
              AND snapshots.entityType = 'account'
              AND snapshots.metricScope = ?
              AND snapshots.periodStart >= ?
              AND snapshots.periodEnd <= ?
              AND snapshots.periodStart = snapshots.periodEnd
              AND runs.status IN ('success', 'partial_success')
        )
        SELECT metricKey, metricValue, metricUnit, metricScope,
               periodStart, periodEnd, observedAt, sourceMode, finishedAt
        FROM ranked
        WHERE rowNumber = 1
        ORDER BY periodStart, metricKey
        """,
        (account_id, scope, period_start, period_end),
    ).fetchall()


def _latest_controlled_timestamp(rows: list, key: str) -> str | None:
    candidates: list[tuple[datetime, str]] = []
    for row in rows:
        value = row[key]
        if type(value) is not str or not value or value != value.strip():
            continue
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            continue
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            continue
        candidates.append((parsed, value))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _period_provenance(rows: list) -> tuple[str | None, str | None, str | None]:
    sources = {
        str(row["sourceMode"])
        for row in rows
        if row["sourceMode"] in ALLOWED_SOURCE_MODES
    }
    if len(sources) == 1:
        source = next(iter(sources))
    elif len(sources) > 1:
        source = "mixed"
    else:
        source = None
    return (
        source,
        _latest_controlled_timestamp(rows, "observedAt"),
        _latest_controlled_timestamp(rows, "finishedAt"),
    )


def account_period_summary(account_id: int, days: int) -> dict:
    account = _account_id(account_id)
    safe_days, period_start, period_end = _period_range(days)
    try:
        with database.connect() as conn:
            daily_rows = _latest_account_rows(
                conn,
                account_id=account,
                period_start=period_start,
                period_end=period_end,
                scope="daily_increment",
            )
            lifetime_rows = _latest_account_rows(
                conn,
                account_id=account,
                period_start=period_start,
                period_end=period_end,
                scope="lifetime_total",
            )
            latest_run = _latest_run_details(conn, account)
    except CollectionFailure:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        raise CollectionFailure("sync_persist_failed") from None

    daily_values: dict[str, list[tuple[str, int | float, str]]] = {}
    contribution_rows: list = []
    for row in daily_rows:
        metric_key = str(row["metricKey"])
        if metric_key not in _ACCOUNT_DAILY_METRICS:
            continue
        contribution_rows.append(row)
        daily_values.setdefault(metric_key, []).append(
            (
                str(row["periodStart"]),
                _public_number(row["metricValue"]),
                str(row["metricUnit"]),
            )
        )

    lifetime_values: dict[str, list[tuple[str, int | float, str]]] = {}
    lifetime_contributors: dict[str, list] = {}
    for row in lifetime_rows:
        metric_key = str(row["metricKey"])
        if metric_key not in _ACCOUNT_LIFETIME_METRICS:
            continue
        lifetime_contributors.setdefault(metric_key, []).append(row)
        lifetime_values.setdefault(metric_key, []).append(
            (
                str(row["periodEnd"]),
                _public_number(row["metricValue"]),
                str(row["metricUnit"]),
            )
        )
    contribution_rows.extend(
        rows[-1] for rows in lifetime_contributors.values() if rows
    )

    metrics: dict[str, dict] = {}
    for metric_key in _ACCOUNT_DAILY_METRICS:
        values = daily_values.get(metric_key, [])
        observed_days = len({day for day, _value, _unit in values})
        if observed_days == safe_days:
            availability = "complete"
        elif observed_days:
            availability = "partial"
        else:
            availability = "missing"
        metrics[metric_key] = {
            "value": (
                _public_number(sum(value for _day, value, _unit in values))
                if values
                else None
            ),
            "scope": "daily_increment",
            "unit": values[-1][2] if values else "count",
            "availability": availability,
            "observedDays": observed_days,
        }
    for metric_key in _ACCOUNT_LIFETIME_METRICS:
        values = lifetime_values.get(metric_key, [])
        if not values:
            availability = "missing"
        elif values[-1][0] == period_end:
            availability = "complete"
        else:
            availability = "partial"
        metrics[metric_key] = {
            "value": values[-1][1] if values else None,
            "scope": "lifetime_total",
            "unit": values[-1][2] if values else "count",
            "availability": availability,
            "observedDays": len({day for day, _value, _unit in values}),
        }
    trusted_source, observed_at, synced_at = _period_provenance(contribution_rows)
    return {
        "accountId": account,
        "days": safe_days,
        "periodStart": period_start,
        "periodEnd": period_end,
        "metrics": metrics,
        "latestRun": latest_run,
        "trustedSourceMode": trusted_source,
        "platformObservedAt": observed_at,
        "localSyncedAt": synced_at,
    }


def account_daily_trends(account_id: int, days: int) -> dict:
    account = _account_id(account_id)
    safe_days, period_start, period_end = _period_range(days)
    try:
        with database.connect() as conn:
            rows = _latest_account_rows(
                conn,
                account_id=account,
                period_start=period_start,
                period_end=period_end,
                scope="daily_increment",
            )
    except CollectionFailure:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        raise CollectionFailure("sync_persist_failed") from None

    by_day: dict[str, dict[str, int | float]] = {}
    for row in rows:
        metric_key = str(row["metricKey"])
        if metric_key not in _ACCOUNT_DAILY_METRICS:
            continue
        by_day.setdefault(str(row["periodStart"]), {})[metric_key] = _public_number(
            row["metricValue"]
        )
    return {
        "accountId": account,
        "days": safe_days,
        "periodStart": period_start,
        "periodEnd": period_end,
        "items": [
            {"date": day, "metrics": by_day[day]}
            for day in sorted(by_day)
        ],
    }


def account_contents(
    account_id: int,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    account = _account_id(account_id)
    safe_limit = _built_in_int(limit, minimum=1, maximum=100)
    safe_offset = _built_in_int(offset, minimum=0)
    try:
        with database.connect() as conn:
            content_state = _content_query_state(conn, account)
            total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM platform_contents WHERE accountId = ?",
                    (account,),
                ).fetchone()[0]
            )
            contents = conn.execute(
                """
                SELECT contentId, title, coverUrl, publishedAt,
                       contentStatus, contentType
                FROM platform_contents
                WHERE accountId = ?
                ORDER BY publishedAt DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (account, safe_limit, safe_offset),
            ).fetchall()
            items = []
            for content in contents:
                metric_rows = conn.execute(
                    """
                    WITH ranked AS (
                        SELECT metricKey, metricValue,
                               ROW_NUMBER() OVER (
                                   PARTITION BY metricKey
                                   ORDER BY periodEnd DESC, id DESC
                               ) AS rowNumber
                        FROM platform_metric_snapshots
                        WHERE accountId = ?
                          AND entityType = 'content'
                          AND entityKey = ?
                          AND metricScope = 'lifetime_total'
                          AND periodStart != ''
                          AND periodEnd != ''
                    )
                    SELECT metricKey, metricValue
                    FROM ranked
                    WHERE rowNumber = 1
                    ORDER BY metricKey
                    """,
                    (account, str(content["contentId"])),
                ).fetchall()
                metrics = {
                    str(row["metricKey"]): _public_number(row["metricValue"])
                    for row in metric_rows
                    if str(row["metricKey"]) in _PUBLIC_CONTENT_METRICS
                }
                items.append(
                    {
                        "contentId": str(content["contentId"]),
                        "title": str(content["title"]),
                        "coverUrl": _public_url(content["coverUrl"]),
                        "publishedAt": str(content["publishedAt"]),
                        "contentStatus": str(content["contentStatus"]),
                        "contentType": str(content["contentType"]),
                        "metrics": metrics,
                    }
                )
    except CollectionFailure:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        raise CollectionFailure("sync_persist_failed") from None
    return {
        "accountId": account,
        **content_state,
        "total": total,
        "limit": safe_limit,
        "offset": safe_offset,
        "items": items,
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
                WHERE accountId = ?
                  AND status IN ('success', 'partial_success')
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
                    WITH ranked AS (
                        SELECT metricKey, metricValue, observedAt,
                               ROW_NUMBER() OVER (
                                   PARTITION BY metricKey
                                   ORDER BY
                                       CASE
                                           WHEN metricScope IN (
                                               'daily_increment',
                                               'lifetime_total'
                                           )
                                            AND periodStart != ''
                                            AND periodEnd != ''
                                           THEN 0
                                           ELSE 1
                                       END,
                                       CASE
                                           WHEN metricScope IN (
                                               'daily_increment',
                                               'lifetime_total'
                                           )
                                           THEN periodEnd
                                           ELSE ''
                                       END DESC,
                                       id DESC
                               ) AS rowNumber
                        FROM platform_metric_snapshots
                        WHERE syncRunId = ?
                          AND entityType = 'account'
                          AND (
                              (
                                  metricScope IN (
                                      'daily_increment',
                                      'lifetime_total'
                                  )
                                  AND periodStart != ''
                                  AND periodEnd != ''
                              )
                              OR (
                                  metricScope = ''
                                  AND periodStart = ''
                                  AND periodEnd = ''
                              )
                          )
                    )
                    SELECT metricKey, metricValue, observedAt
                    FROM ranked
                    WHERE rowNumber = 1
                    ORDER BY metricKey
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
