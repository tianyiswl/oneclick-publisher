# -*- coding: utf-8 -*-
"""抖音评论手动同步、独立洞察事务和匿名面板投影。"""

from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import json
from typing import Callable

from PyQt6.QtCore import QLockFile

from . import database
from .douyin_comment_data_collector import DouyinCommentDataCollector
from .paths import USER_DATA_DIR
from .platform_data_collection_errors import CleanupReceipt
from .platform_data_comment_models import (
    ALLOWED_COMMENT_ERROR_CODES,
    ALLOWED_COMMENT_LABELS,
    CommentCollectionBatch,
    CommentInsightFailure,
    CommentRecord,
    InsightResult,
)
from . import platform_data_comment_store as store


COMMENT_PROGRESS = {
    "preparing": "正在准备只读会话",
    "collecting": "正在读取评论",
    "validating": "正在校验评论",
    "persisting": "正在保存评论",
    "insight": "正在生成洞察",
    "completed": "评论同步完成",
    "failed": "评论同步未完成",
}

_AI_ERROR_CODES = frozenset(
    {
        "comment_ai_not_configured",
        "comment_ai_timeout",
        "comment_ai_service_unavailable",
        "comment_ai_response_invalid",
        "comment_ai_evidence_invalid",
    }
)
_SYNC_ERROR_CODES = frozenset(
    {
        "comment_content_unavailable",
        "comment_login_required",
        "comment_verification_required",
        "comment_access_denied",
        "comment_payload_invalid",
        "comment_sync_timeout",
        "comment_sync_cancelled",
    }
)
_PUBLIC_RESULT_KEYS = (
    "status",
    "errorCode",
    "acceptedCount",
    "insertedCount",
    "updatedCount",
    "rejectedCount",
    "pageCount",
    "stopReason",
    "warningCode",
    "cleanup",
    "aiStatus",
    "aiErrorCode",
)
_DEFAULT_PROMPT_VERSION = "comment-insight-v1"
_DEFAULT_SCHEMA_VERSION = 1
_MAX_INSIGHT_JSON_BYTES = 1_000_000
_MAX_CONTENT_ID_LENGTH = 512
_COMMENT_SYNC_LEASE_WAIT_MS = 100
_COMMENT_SYNC_LEASE_STALE_MS = 30_000
_PROCESS_CONTROL = (asyncio.CancelledError, KeyboardInterrupt, SystemExit)


def _payload_failure() -> CommentInsightFailure:
    return CommentInsightFailure("comment_payload_invalid")


def _valid_account_id(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise _payload_failure()
    return value


def _valid_content_id(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > _MAX_CONTENT_ID_LENGTH
        or value != value.strip()
    ):
        raise _payload_failure()
    return value


def _valid_report(value: object) -> Callable[[dict], None] | None:
    if value is not None and not callable(value):
        raise _payload_failure()
    return value


def _valid_connect_factory(value: object):
    if not callable(value):
        raise _payload_failure()
    return value


def _new_comment_sync_lease(account_id: int, content_id: str):
    """使用匿名稳定路径创建跨进程评论同步租约。"""

    root = USER_DATA_DIR / "locks"
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    digest.update(str(account_id).encode("ascii"))
    digest.update(b"\x00")
    digest.update(content_id.encode("utf-8"))
    lock = QLockFile(str(root / f"comment-sync-{digest.hexdigest()}.lock"))
    lock.setStaleLockTime(_COMMENT_SYNC_LEASE_STALE_MS)
    return lock


def _service_control(value: BaseException) -> tuple[str, int | None]:
    if isinstance(value, asyncio.CancelledError):
        return "cancelled", None
    if isinstance(value, KeyboardInterrupt):
        return "keyboard_interrupt", None
    code = value.code if isinstance(value, SystemExit) else 1
    if type(code) is not int or not -2_147_483_648 <= code <= 2_147_483_647:
        code = 1
    return "system_exit", code


def _raise_service_control(control: tuple[str, int | None]) -> None:
    kind, code = control
    if kind == "cancelled":
        raise asyncio.CancelledError() from None
    if kind == "keyboard_interrupt":
        raise KeyboardInterrupt() from None
    raise SystemExit(code) from None


def _release_comment_sync_lease(lock) -> tuple[str, object]:
    try:
        unlock = getattr(lock, "unlock", None)
        if not callable(unlock):
            raise RuntimeError("comment sync lease unavailable")
        unlock()
    except _PROCESS_CONTROL as exc:
        control = _service_control(exc)
        exc = None
        return "control", control
    except BaseException:
        return "failure", None
    return "ok", None


def _acquire_comment_sync_lease(
    lease_factory,
    account_id: int,
    content_id: str,
):
    lock = None
    try:
        lock = lease_factory(account_id, content_id)
        try_lock = getattr(lock, "tryLock", None)
        unlock = getattr(lock, "unlock", None)
        if not callable(try_lock) or not callable(unlock):
            raise RuntimeError("comment sync lease unavailable")
        acquired = try_lock(_COMMENT_SYNC_LEASE_WAIT_MS)
        if type(acquired) is not bool or not acquired:
            return None
        return lock
    except _PROCESS_CONTROL as exc:
        control = _service_control(exc)
        exc = None
        if lock is not None:
            _release_comment_sync_lease(lock)
        lock = None
        _raise_service_control(control)
    except BaseException:
        if lock is not None:
            release_outcome = _release_comment_sync_lease(lock)
            lock = None
            if release_outcome[0] == "control":
                _raise_service_control(release_outcome[1])
        return None


def _emit(report: Callable[[dict], None] | None, stage: str) -> None:
    if report is None:
        return
    event = {"stage": stage, "message": COMMENT_PROGRESS[stage]}
    try:
        report(event)
    except Exception:
        return


def _cleanup_payload(receipt: object, *, no_session: bool = False) -> dict:
    if type(receipt) is CleanupReceipt:
        return receipt.public_payload()
    if no_session:
        return {"closed": True, "aliveResourceCount": 0}
    return {"closed": False, "aliveResourceCount": 1}


def _public_result(
    *,
    status: str,
    error_code: str = "",
    accepted_count: int = 0,
    inserted_count: int = 0,
    updated_count: int = 0,
    rejected_count: int = 0,
    page_count: int = 0,
    stop_reason: str = "",
    warning_code: str = "",
    cleanup: object = None,
    no_session: bool = False,
    ai_status: str = "skipped",
    ai_error_code: str = "",
) -> dict:
    if status != "success":
        status = "failed"
        if type(error_code) is not str or error_code not in ALLOWED_COMMENT_ERROR_CODES:
            error_code = "comment_payload_invalid"
        accepted_count = inserted_count = updated_count = rejected_count = 0
        page_count = 0
        stop_reason = warning_code = ""
        ai_status = "skipped"
        ai_error_code = ""
    else:
        error_code = ""
        if ai_status not in {"success", "failed", "skipped"}:
            ai_status = "failed"
            ai_error_code = "comment_ai_response_invalid"
        if ai_status == "success":
            ai_error_code = ""
        elif type(ai_error_code) is not str or ai_error_code not in _AI_ERROR_CODES:
            ai_status = "failed"
            ai_error_code = "comment_ai_response_invalid"
    result = {
        "status": status,
        "errorCode": error_code,
        "acceptedCount": accepted_count,
        "insertedCount": inserted_count,
        "updatedCount": updated_count,
        "rejectedCount": rejected_count,
        "pageCount": page_count,
        "stopReason": stop_reason,
        "warningCode": warning_code,
        "cleanup": _cleanup_payload(cleanup, no_session=no_session),
        "aiStatus": ai_status,
        "aiErrorCode": ai_error_code,
    }
    # Keep this boundary explicit so later callers cannot accidentally add IDs.
    return {key: result[key] for key in _PUBLIC_RESULT_KEYS}


def _account_and_content(conn, account_id: int, content_id: str) -> tuple[dict, dict] | None:
    content = store.content_for_comment_sync(conn, account_id, content_id)
    if content is None:
        return None
    cursor = conn.execute(
        """
        SELECT id, type, filePath FROM user_info
        WHERE id = ? AND type = 3
        LIMIT 1
        """,
        (account_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    if hasattr(row, "keys"):
        account = {name: row[name] for name in ("id", "type", "filePath")}
    else:
        account = dict(zip(("id", "type", "filePath"), row, strict=True))
    if (
        type(account.get("id")) is not int
        or account["id"] != account_id
        or type(account.get("type")) is not int
        or account["type"] != 3
        or type(account.get("filePath")) is not str
        or not account["filePath"]
    ):
        return None
    return account, content


def _record_failed_run(
    connect_factory,
    *,
    account_id: int,
    content_id: str,
    source_mode: str,
    error_code: str,
    cancelled: bool = False,
) -> None:
    try:
        with connect_factory() as conn:
            store.record_failed_comment_sync(
                conn,
                account_id,
                content_id,
                source_mode,
                error_code,
                cancelled=cancelled,
            )
    except _PROCESS_CONTROL:
        raise
    except BaseException:
        return


def _batch_error(batch: object, content_id: str) -> str:
    if type(batch) is not CommentCollectionBatch or batch.content_id != content_id:
        return "comment_payload_invalid"
    if type(batch.cleanup_receipt) is not CleanupReceipt:
        return "comment_payload_invalid"
    if not batch.cleanup_receipt.closed:
        return "comment_sync_cancelled"
    if batch.stop_reason in {"known_comment", "platform_end"}:
        return "" if not batch.warning_code else "comment_payload_invalid"
    if (
        batch.stop_reason == "limit_reached"
        and batch.warning_code == "comment_limit_reached"
        and batch.accepted_count == 100
    ):
        return ""
    return "comment_payload_invalid"


def _comment_fingerprint(comments: tuple[CommentRecord, ...]) -> str:
    digest = hashlib.sha256()
    for item in comments:
        digest.update(item.comment_key.encode("ascii"))
        digest.update(b"\x1f")
        digest.update(item.body.encode("utf-8"))
        digest.update(b"\x1e")
    return digest.hexdigest()


def _records_from_rows(rows: list[dict]) -> tuple[CommentRecord, ...]:
    records = []
    for row in rows:
        records.append(
            CommentRecord(
                content_id=row["contentId"],
                comment_key=row["commentKey"],
                body=row["body"],
                like_count=row["likeCount"],
                reply_count=row["replyCount"],
                commented_at=row["commentedAt"],
                observed_at=row["lastSeenAt"],
            )
        )
    return tuple(records)


def _saved_comment_records(
    connect_factory,
    account_id: int,
    content_id: str,
) -> tuple[CommentRecord, ...]:
    with connect_factory() as conn:
        rows = store.list_comment_rows(conn, account_id, content_id, limit=100)
    return _records_from_rows(rows)


def _valid_ai_metadata(
    model_name: object,
    prompt_version: object,
    schema_version: object,
) -> tuple[str, str, int]:
    if (
        type(model_name) is not str
        or not model_name
        or model_name != model_name.strip()
        or type(prompt_version) is not str
        or not prompt_version
        or prompt_version != prompt_version.strip()
        or type(schema_version) is not int
        or schema_version <= 0
    ):
        raise _payload_failure()
    return model_name, prompt_version, schema_version


def _persist_skipped_insight(
    conn,
    *,
    account_id: int,
    content_id: str,
    model_name: str,
    prompt_version: str,
    schema_version: int,
    input_fingerprint: str,
    comment_count: int,
) -> dict:
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO comment_insight_runs
            (accountId, platformType, contentId, status, errorCode, providerType,
             modelName, promptVersion, schemaVersion, inputFingerprint,
             commentCount, classificationsJson, candidatesJson, startedAt, finishedAt)
        VALUES (?, 3, ?, 'skipped', 'comment_ai_not_configured',
                'openai_compatible', ?, ?, ?, ?, ?, '[]', '[]', ?, ?)
        """,
        (
            account_id,
            content_id,
            model_name,
            prompt_version,
            schema_version,
            input_fingerprint,
            comment_count,
            timestamp,
            timestamp,
        ),
    )
    return {"status": "skipped", "errorCode": "comment_ai_not_configured"}


def record_comment_ai_outcome(
    account_id: int,
    content_id: str,
    *,
    result: InsightResult | None,
    error_code: str,
    model_name: str,
    prompt_version: str,
    schema_version: int,
    input_fingerprint: str,
    comment_count: int,
    connect_factory=database.connect,
) -> dict:
    """在评论事务之后，单独保存一个受控的 AI 成功或失败结果。"""

    account_id = _valid_account_id(account_id)
    content_id = _valid_content_id(content_id)
    connect_factory = _valid_connect_factory(connect_factory)
    model_name, prompt_version, schema_version = _valid_ai_metadata(
        model_name, prompt_version, schema_version
    )
    if type(error_code) is not str:
        raise _payload_failure()
    if result is None:
        if error_code not in _AI_ERROR_CODES:
            raise _payload_failure()
    elif type(result) is not InsightResult or error_code:
        raise _payload_failure()
    try:
        with connect_factory() as conn:
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            if store.content_for_comment_sync(conn, account_id, content_id) is None:
                raise CommentInsightFailure("comment_content_unavailable")
            rows = store.list_comment_rows(conn, account_id, content_id, limit=100)
            comments = _records_from_rows(rows)
            actual_fingerprint = _comment_fingerprint(comments)
            actual_count = len(comments)
            if (
                type(input_fingerprint) is not str
                or input_fingerprint != actual_fingerprint
                or type(comment_count) is not int
                or comment_count != actual_count
            ):
                raise _payload_failure()
            if result is not None and result.known_comment_keys != frozenset(
                item.comment_key for item in comments
            ):
                raise CommentInsightFailure("comment_ai_evidence_invalid")
            if error_code == "comment_ai_not_configured":
                receipt = _persist_skipped_insight(
                    conn,
                    account_id=account_id,
                    content_id=content_id,
                    model_name=model_name,
                    prompt_version=prompt_version,
                    schema_version=schema_version,
                    input_fingerprint=actual_fingerprint,
                    comment_count=actual_count,
                )
            else:
                receipt = store.persist_insight_result(
                    conn,
                    account_id,
                    content_id,
                    provider_type="openai_compatible",
                    model_name=model_name,
                    prompt_version=prompt_version,
                    schema_version=schema_version,
                    input_fingerprint=actual_fingerprint,
                    comment_count=actual_count,
                    result=result,
                    error_code=error_code,
                )
            status = receipt.get("status")
            saved_error = receipt.get("errorCode")
            if status not in {"success", "failed", "skipped"}:
                raise _payload_failure()
            if status == "skipped":
                if saved_error != "comment_ai_not_configured":
                    raise _payload_failure()
            elif type(saved_error) is not str or (
                saved_error and saved_error not in _AI_ERROR_CODES
            ):
                raise _payload_failure()
            return {"status": status, "errorCode": saved_error}
    except _PROCESS_CONTROL:
        raise
    except CommentInsightFailure as exc:
        raise CommentInsightFailure(exc.error_code) from None
    except BaseException:
        raise _payload_failure() from None


def _run_optional_ai_after_commit(
    *,
    account_id: int,
    content: dict,
    ai_provider_factory,
    connect_factory,
) -> dict:
    comments = _saved_comment_records(
        connect_factory, account_id, content["contentId"]
    )
    fingerprint = _comment_fingerprint(comments)

    def persist_failure(
        code: str,
        *,
        model_name: str = "unavailable",
        prompt_version: str = _DEFAULT_PROMPT_VERSION,
        schema_version: int = _DEFAULT_SCHEMA_VERSION,
    ) -> dict:
        try:
            return record_comment_ai_outcome(
                account_id,
                content["contentId"],
                result=None,
                error_code=code,
                model_name=model_name,
                prompt_version=prompt_version,
                schema_version=schema_version,
                input_fingerprint=fingerprint,
                comment_count=len(comments),
                connect_factory=connect_factory,
            )
        except _PROCESS_CONTROL:
            raise
        except BaseException:
            return {"status": "failed", "errorCode": code}

    provider = None
    try:
        provider = ai_provider_factory()
    except _PROCESS_CONTROL:
        raise
    except CommentInsightFailure as exc:
        code = (
            exc.error_code
            if exc.error_code in _AI_ERROR_CODES
            else "comment_ai_service_unavailable"
        )
        if code == "comment_ai_not_configured":
            return persist_failure(code, model_name="unconfigured")
        return persist_failure(code)
    except BaseException:
        provider = None
        return persist_failure("comment_ai_service_unavailable")
    if provider is None:
        return persist_failure(
            "comment_ai_not_configured", model_name="unconfigured"
        )
    try:
        model_name = getattr(provider, "model_name", None)
        prompt_version = getattr(
            provider, "prompt_version", _DEFAULT_PROMPT_VERSION
        )
        schema_version = getattr(
            provider, "schema_version", _DEFAULT_SCHEMA_VERSION
        )
    except _PROCESS_CONTROL:
        raise
    except BaseException:
        return persist_failure("comment_ai_response_invalid")
    if (
        type(model_name) is not str
        or not model_name
        or model_name != model_name.strip()
        or type(prompt_version) is not str
        or not prompt_version
        or prompt_version != prompt_version.strip()
        or type(schema_version) is not int
        or schema_version <= 0
    ):
        return persist_failure("comment_ai_response_invalid")
    try:
        insight = provider.analyze(content["title"], comments)
        if type(insight) is not InsightResult:
            raise CommentInsightFailure("comment_ai_response_invalid")
        return record_comment_ai_outcome(
            account_id,
            content["contentId"],
            result=insight,
            error_code="",
            model_name=model_name,
            prompt_version=prompt_version,
            schema_version=schema_version,
            input_fingerprint=fingerprint,
            comment_count=len(comments),
            connect_factory=connect_factory,
        )
    except _PROCESS_CONTROL:
        raise
    except CommentInsightFailure as exc:
        code = (
            exc.error_code
            if exc.error_code in _AI_ERROR_CODES
            else "comment_ai_response_invalid"
        )
    except BaseException:
        code = "comment_ai_service_unavailable"
    provider = None
    return persist_failure(
        code,
        model_name=model_name,
        prompt_version=prompt_version,
        schema_version=schema_version,
    )


def _sync_comments_owned(
    account_id: int,
    content_id: str,
    report=None,
    *,
    collector_factory=DouyinCommentDataCollector,
    ai_provider_factory=lambda: None,
    connect_factory=database.connect,
) -> dict:
    """按一次用户动作执行一次评论采集，不重试也不负责 UI 去重。"""

    try:
        account_id = _valid_account_id(account_id)
        content_id = _valid_content_id(content_id)
        report = _valid_report(report)
        if not callable(collector_factory) or not callable(ai_provider_factory):
            raise _payload_failure()
        connect_factory = _valid_connect_factory(connect_factory)
    except CommentInsightFailure:
        return _public_result(
            status="failed",
            error_code="comment_content_unavailable",
            no_session=True,
        )

    _emit(report, "preparing")
    try:
        with connect_factory() as conn:
            located = _account_and_content(conn, account_id, content_id)
            if located is None:
                raise CommentInsightFailure("comment_content_unavailable")
            account, content = located
            known_keys = store.known_comment_keys(conn, account_id, content_id)
    except _PROCESS_CONTROL:
        raise
    except CommentInsightFailure as exc:
        _emit(report, "failed")
        return _public_result(
            status="failed",
            error_code=exc.error_code,
            no_session=True,
        )
    except BaseException:
        _emit(report, "failed")
        return _public_result(
            status="failed",
            error_code="comment_payload_invalid",
            no_session=True,
        )

    _emit(report, "collecting")
    batch = None
    try:
        collector = collector_factory()
        collect = getattr(collector, "collect", None)
        if not callable(collect):
            raise _payload_failure()
        # Collector diagnostics stay internal; the UI receives only fixed stages.
        batch = collect(
            account,
            content_id,
            known_keys,
            limit=100,
            report=None,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except asyncio.CancelledError as exc:
        receipt = getattr(exc, "cleanup_receipt", None)
        _record_failed_run(
            connect_factory,
            account_id=account_id,
            content_id=content_id,
            source_mode="browser_signed",
            error_code="comment_sync_cancelled",
            cancelled=True,
        )
        _emit(report, "failed")
        return _public_result(
            status="failed",
            error_code="comment_sync_cancelled",
            cleanup=receipt,
        )
    except CommentInsightFailure as exc:
        receipt = getattr(exc, "cleanup_receipt", None)
        error_code = (
            exc.error_code
            if exc.error_code in _SYNC_ERROR_CODES
            else "comment_payload_invalid"
        )
        _record_failed_run(
            connect_factory,
            account_id=account_id,
            content_id=content_id,
            source_mode="browser_signed",
            error_code=error_code,
            cancelled=error_code == "comment_sync_cancelled",
        )
        _emit(report, "failed")
        return _public_result(
            status="failed",
            error_code=error_code,
            cleanup=receipt,
        )
    except BaseException as exc:
        receipt = getattr(exc, "cleanup_receipt", None)
        _record_failed_run(
            connect_factory,
            account_id=account_id,
            content_id=content_id,
            source_mode="browser_signed",
            error_code="comment_payload_invalid",
        )
        _emit(report, "failed")
        return _public_result(
            status="failed",
            error_code="comment_payload_invalid",
            cleanup=receipt,
        )

    _emit(report, "validating")
    batch_error = _batch_error(batch, content_id)
    if batch_error:
        source_mode = (
            batch.source_mode
            if type(batch) is CommentCollectionBatch
            else "browser_signed"
        )
        receipt = (
            batch.cleanup_receipt
            if type(batch) is CommentCollectionBatch
            else None
        )
        _record_failed_run(
            connect_factory,
            account_id=account_id,
            content_id=content_id,
            source_mode=source_mode,
            error_code=batch_error,
            cancelled=batch_error == "comment_sync_cancelled",
        )
        _emit(report, "failed")
        return _public_result(
            status="failed",
            error_code=batch_error,
            cleanup=receipt,
        )

    assert type(batch) is CommentCollectionBatch
    _emit(report, "persisting")
    try:
        with connect_factory() as conn:
            receipt = store.persist_comment_batch(conn, account_id, batch)
    except _PROCESS_CONTROL:
        raise
    except BaseException:
        _record_failed_run(
            connect_factory,
            account_id=account_id,
            content_id=content_id,
            source_mode=batch.source_mode,
            error_code="comment_payload_invalid",
        )
        _emit(report, "failed")
        return _public_result(
            status="failed",
            error_code="comment_payload_invalid",
            cleanup=batch.cleanup_receipt,
        )

    _emit(report, "insight")
    try:
        ai_receipt = _run_optional_ai_after_commit(
            account_id=account_id,
            content=content,
            ai_provider_factory=ai_provider_factory,
            connect_factory=connect_factory,
        )
    except _PROCESS_CONTROL:
        raise
    except BaseException:
        ai_receipt = {
            "status": "failed",
            "errorCode": "comment_ai_service_unavailable",
        }
    _emit(report, "completed")
    return _public_result(
        status="success",
        accepted_count=receipt["acceptedCount"],
        inserted_count=receipt["insertedCount"],
        updated_count=receipt["updatedCount"],
        rejected_count=receipt["rejectedCount"],
        page_count=receipt["pageCount"],
        stop_reason=receipt["stopReason"],
        warning_code=batch.warning_code,
        cleanup=batch.cleanup_receipt,
        ai_status=ai_receipt["status"],
        ai_error_code=ai_receipt["errorCode"],
    )


def sync_comments(
    account_id: int,
    content_id: str,
    report=None,
    *,
    collector_factory=DouyinCommentDataCollector,
    ai_provider_factory=lambda: None,
    connect_factory=database.connect,
    lease_factory=_new_comment_sync_lease,
) -> dict:
    """在首次读库前为同一账号和作品取得跨进程单活租约。"""

    try:
        account_id = _valid_account_id(account_id)
        content_id = _valid_content_id(content_id)
        report = _valid_report(report)
        if (
            not callable(collector_factory)
            or not callable(ai_provider_factory)
            or not callable(lease_factory)
        ):
            raise _payload_failure()
        connect_factory = _valid_connect_factory(connect_factory)
    except CommentInsightFailure:
        return _public_result(
            status="failed",
            error_code="comment_content_unavailable",
            no_session=True,
        )

    lease = _acquire_comment_sync_lease(
        lease_factory, account_id, content_id
    )
    if lease is None:
        return _public_result(
            status="failed",
            error_code="comment_sync_timeout",
            no_session=True,
        )

    result = None
    operation_control = None
    try:
        try:
            result = _sync_comments_owned(
                account_id,
                content_id,
                report,
                collector_factory=collector_factory,
                ai_provider_factory=ai_provider_factory,
                connect_factory=connect_factory,
            )
        except _PROCESS_CONTROL as exc:
            operation_control = _service_control(exc)
            exc = None
        except BaseException:
            result = _public_result(
                status="failed",
                error_code="comment_payload_invalid",
            )
    finally:
        release_outcome = _release_comment_sync_lease(lease)
        lease = None
    if operation_control is not None:
        result = None
        _raise_service_control(operation_control)
    if release_outcome[0] == "control":
        result = None
        _raise_service_control(release_outcome[1])
    if release_outcome[0] != "ok":
        return _public_result(
            status="failed",
            error_code="comment_sync_cancelled",
        )
    if type(result) is not dict:
        return _public_result(
            status="failed",
            error_code="comment_payload_invalid",
        )
    return result


def _safe_insight_payload(
    latest: object,
    rows: list[dict],
    current_fingerprint: str,
    current_count: int,
) -> tuple[dict[str, list[str]], list[dict], str, str]:
    if latest is None:
        return {}, [], "skipped", "comment_ai_not_configured"
    if type(latest) is not dict:
        return {}, [], "failed", "comment_ai_response_invalid"
    if (
        latest.get("inputFingerprint") != current_fingerprint
        or type(latest.get("commentCount")) is not int
        or latest.get("commentCount") != current_count
    ):
        return {}, [], "failed", "comment_ai_response_invalid"
    status = latest.get("status")
    if status == "skipped":
        if latest.get("errorCode") == "comment_ai_not_configured":
            return {}, [], "skipped", "comment_ai_not_configured"
        return {}, [], "failed", "comment_ai_response_invalid"
    if status == "failed":
        code = latest.get("errorCode")
        if type(code) is not str or code not in _AI_ERROR_CODES:
            code = "comment_ai_response_invalid"
        return {}, [], "failed", code
    if status != "success" or latest.get("errorCode") != "":
        return {}, [], "failed", "comment_ai_response_invalid"
    classifications_json = latest.get("classificationsJson")
    candidates_json = latest.get("candidatesJson")
    if (
        type(classifications_json) is not str
        or type(candidates_json) is not str
        or len(classifications_json) > _MAX_INSIGHT_JSON_BYTES
        or len(candidates_json) > _MAX_INSIGHT_JSON_BYTES
    ):
        return {}, [], "failed", "comment_ai_response_invalid"
    try:
        if (
            len(classifications_json.encode("utf-8")) > _MAX_INSIGHT_JSON_BYTES
            or len(candidates_json.encode("utf-8")) > _MAX_INSIGHT_JSON_BYTES
        ):
            return {}, [], "failed", "comment_ai_response_invalid"
        classifications = json.loads(classifications_json)
        candidates = json.loads(candidates_json)
    except (TypeError, ValueError, RecursionError, json.JSONDecodeError):
        return {}, [], "failed", "comment_ai_response_invalid"
    known = {row.get("commentKey") for row in rows}
    if (
        type(classifications) is not list
        or len(classifications) > 100
        or type(candidates) is not list
        or len(candidates) > 5
    ):
        return {}, [], "failed", "comment_ai_response_invalid"
    label_map: dict[str, list[str]] = {}
    for item in classifications:
        if type(item) is not dict or set(item) != {"commentKey", "labels"}:
            return {}, [], "failed", "comment_ai_response_invalid"
        key = item.get("commentKey")
        labels = item.get("labels")
        if (
            type(key) is not str
            or key not in known
            or key in label_map
            or type(labels) is not list
            or not labels
            or any(
                type(label) is not str or label not in ALLOWED_COMMENT_LABELS
                for label in labels
            )
            or len(set(labels)) != len(labels)
        ):
            return {}, [], "failed", "comment_ai_response_invalid"
        label_map[key] = list(labels)
    safe_candidates = []
    for item in candidates:
        if type(item) is not dict or set(item) != {"title", "reason", "evidenceKeys"}:
            return {}, [], "failed", "comment_ai_response_invalid"
        title = item.get("title")
        reason = item.get("reason")
        keys = item.get("evidenceKeys")
        if (
            type(title) is not str
            or not title
            or title != title.strip()
            or type(reason) is not str
            or not reason
            or reason != reason.strip()
            or type(keys) is not list
            or not keys
            or len(keys) > 100
            or any(type(key) is not str or key not in known for key in keys)
            or len(set(keys)) != len(keys)
        ):
            return {}, [], "failed", "comment_ai_response_invalid"
        safe_candidates.append(
            {"title": title, "reason": reason, "evidenceKeys": list(keys)}
        )
    return label_map, safe_candidates, "success", ""


def comment_panel_payload(
    account_id: int,
    content_id: str,
    label: str = "全部",
    limit: int = 100,
    *,
    connect_factory=database.connect,
) -> dict:
    """返回 UI 可显示的评论和证据，不暴露数据库或平台标识。"""

    account_id = _valid_account_id(account_id)
    content_id = _valid_content_id(content_id)
    if type(label) is not str or label not in ({"全部"} | set(ALLOWED_COMMENT_LABELS)):
        raise _payload_failure()
    if type(limit) is not int or limit < 1 or limit > 100:
        raise _payload_failure()
    connect_factory = _valid_connect_factory(connect_factory)
    try:
        with connect_factory() as conn:
            content = store.content_for_comment_sync(conn, account_id, content_id)
            if content is None:
                raise CommentInsightFailure("comment_content_unavailable")
            rows = store.list_comment_rows(conn, account_id, content_id, limit=100)
            latest = store.latest_insight(conn, account_id, content_id)
            sync_row = conn.execute(
                """
                SELECT finishedAt FROM platform_comment_sync_runs
                WHERE accountId = ? AND platformType = 3 AND contentId = ?
                  AND status = 'success'
                ORDER BY id DESC LIMIT 1
                """,
                (account_id, content_id),
            ).fetchone()
        last_sync_at = ""
        if sync_row is not None:
            last_sync_at = (
                sync_row["finishedAt"]
                if hasattr(sync_row, "keys")
                else sync_row[0]
            )
            if type(last_sync_at) is not str:
                last_sync_at = ""
        comments = _records_from_rows(rows)
        label_map, candidates, ai_status, ai_error_code = _safe_insight_payload(
            latest,
            rows,
            _comment_fingerprint(comments),
            len(comments),
        )

        projected_by_key: dict[str, dict] = {}
        all_comments = []
        for index, row in enumerate(rows, start=1):
            key = row.get("commentKey")
            labels = label_map.get(key, [])
            item = {
                "ref": f"C{index:03d}",
                "body": row.get("body"),
                "likeCount": row.get("likeCount"),
                "replyCount": row.get("replyCount"),
                "commentedAt": row.get("commentedAt"),
                "labels": labels,
            }
            projected_by_key[key] = item
            if label == "全部" or label in labels:
                all_comments.append(dict(item))
        safe_candidates = []
        for candidate in candidates:
            safe_candidates.append(
                {
                    "title": candidate["title"],
                    "reason": candidate["reason"],
                    "evidence": [
                        dict(projected_by_key[key])
                        for key in candidate["evidenceKeys"]
                    ],
                }
            )
        return {
            "title": content["title"],
            "lastSyncAt": last_sync_at,
            "comments": all_comments[:limit],
            "candidates": safe_candidates,
            "aiStatus": ai_status,
            "aiErrorCode": ai_error_code,
        }
    except _PROCESS_CONTROL:
        raise
    except CommentInsightFailure as exc:
        raise CommentInsightFailure(exc.error_code) from None
    except BaseException:
        raise _payload_failure() from None
