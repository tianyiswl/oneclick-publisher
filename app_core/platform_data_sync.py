# -*- coding: utf-8 -*-
"""平台数据同步编排。"""

from __future__ import annotations

from collections.abc import Callable

from . import account_service, platform_data_service
from .platform_data_collection_errors import (
    PlatformDataCollectionError,
    public_failure_diagnostic,
)
from .platform_data_collectors import collector_for_platform
from .platform_data_models import CollectionBatch, CollectionFailure
from .xiaohongshu_data_collector import official_visible_readback


_PROGRESS_MESSAGES = {
    "direct_session": "正在读取已登录账号数据",
    "browser_signed": "正在读取平台官方数据…",
    "account_metrics": "正在整理账号核心指标",
    "content_list": "正在读取作品列表",
    "content_metrics": "正在整理作品指标",
    "persisting": "正在保存可信指标",
    "completed": "数据同步完成",
    "partial": "部分数据已保存",
    "failed": "数据同步未完成",
}
_FINAL_STAGE_BY_STATUS = {
    "success": "completed",
    "partial_success": "partial",
    "failed": "failed",
}
_PUBLIC_ERROR_CODES = platform_data_service.ALLOWED_ERROR_CODES


def _account(account_id: object) -> dict:
    if type(account_id) is not int or account_id <= 0:
        raise CollectionFailure("collector_not_available")
    for account in account_service.list_accounts():
        if type(account.get("id")) is int and account["id"] == account_id:
            return dict(account)
    raise CollectionFailure("collector_not_available")


def _emit(report: Callable[[dict], None] | None, stage: str, message: str) -> None:
    if report is None:
        return
    try:
        report({"stage": stage, "message": message})
    except Exception:
        return


def _report_stage(report: Callable[[dict], None] | None, stage: str) -> None:
    _emit(report, stage, _PROGRESS_MESSAGES[stage])


def _public_error_code(value: object) -> str:
    if type(value) is str and value in _PUBLIC_ERROR_CODES:
        return value
    return "metric_payload_invalid"


def _public_result(
    saved: dict,
    *,
    account_id: int,
    source_mode: str,
    metric_count: int,
    content_count: int,
    diagnostics: dict | None = None,
) -> dict:
    status = saved.get("status")
    if status == "success":
        error_code = ""
    elif status == "partial_success":
        error_code = _public_error_code(saved.get("errorCode"))
    else:
        status = "failed"
        error_code = _public_error_code(saved.get("errorCode"))
    result = {
        "accountId": account_id,
        "status": status,
        "sourceMode": source_mode,
        "errorCode": error_code,
        "metricCount": metric_count,
        "contentCount": content_count,
    }
    if diagnostics is not None:
        result["diagnostics"] = diagnostics
    return result


def _public_diagnostics(value: object) -> dict | None:
    if type(value) is not dict or set(value) not in (
        {"cleanup"},
        {"cleanup", "validation"},
        {"cleanup", "failure"},
    ):
        return None
    cleanup = value.get("cleanup")
    if type(cleanup) is not dict or set(cleanup) != {"closed", "aliveResourceCount"}:
        return None
    closed = cleanup.get("closed")
    alive = cleanup.get("aliveResourceCount")
    if (
        type(closed) is not bool
        or type(alive) is not int
        or alive < 0
        or closed != (alive == 0)
    ):
        return None
    result = {"cleanup": {"closed": closed, "aliveResourceCount": alive}}
    failure = value.get("failure")
    if failure is not None:
        safe_failure = public_failure_diagnostic(failure)
        if safe_failure is None:
            return None
        result["failure"] = safe_failure
        return result
    validation = value.get("validation")
    if validation is None:
        return result
    if (
        type(validation) is not dict
        or set(validation) != {"mode", "officialVisible"}
        or validation.get("mode") != "official_visible_readback"
    ):
        return None
    official = validation.get("officialVisible")
    if (
        type(official) is not dict
        or set(official) != {"account", "content"}
        or type(official.get("account")) is not dict
        or set(official["account"]) != {"followersTotal"}
        or type(official.get("content")) is not dict
        or set(official["content"]) != {"contentId", "views"}
    ):
        return None
    followers = official["account"].get("followersTotal")
    content_id = official["content"].get("contentId")
    views = official["content"].get("views")
    if (
        type(followers) not in (int, float)
        or type(content_id) is not str
        or type(views) not in (int, float)
    ):
        return None
    result["validation"] = {
        "mode": "official_visible_readback",
        "officialVisible": {
            "account": {"followersTotal": followers},
            "content": {"contentId": content_id, "views": views},
        },
    }
    return result


def _atomic_validation_result(value: object, official_expected: object) -> dict | None:
    if type(value) is not dict or set(value) != {
        "officialVisible", "localReadback", "matches"
    }:
        return None
    expected = _public_diagnostics(
        {
            "cleanup": {"closed": True, "aliveResourceCount": 0},
            "validation": {
                "mode": "official_visible_readback",
                "officialVisible": official_expected,
            },
        }
    )
    if expected is None:
        return None
    official = expected["validation"]["officialVisible"]
    if value.get("officialVisible") != official:
        return None
    local = value.get("localReadback")
    matches = value.get("matches")
    if (
        type(local) is not dict
        or set(local) != {"account", "content"}
        or type(local.get("account")) is not dict
        or set(local["account"]) != {"followersTotal"}
        or type(local.get("content")) is not dict
        or set(local["content"]) != {"contentId", "views"}
        or type(matches) is not dict
        or set(matches) != {"followersTotal", "views"}
        or type(matches.get("followersTotal")) is not bool
        or type(matches.get("views")) is not bool
    ):
        return None
    local_followers = local["account"].get("followersTotal")
    local_content_id = local["content"].get("contentId")
    local_views = local["content"].get("views")
    if (
        type(local_followers) not in (int, float)
        or type(local_content_id) is not str
        or type(local_views) not in (int, float)
        or local_followers != official["account"]["followersTotal"]
        or local_content_id != official["content"]["contentId"]
        or local_views != official["content"]["views"]
        or not all(matches.values())
    ):
        return None
    return {
        "officialVisible": official,
        "localReadback": {
            "account": {"followersTotal": local_followers},
            "content": {"contentId": local_content_id, "views": local_views},
        },
        "matches": {"followersTotal": True, "views": True},
    }


def _error_diagnostics(error: PlatformDataCollectionError) -> dict | None:
    receipt = error.cleanup_receipt
    if receipt is None:
        return None
    value = {"cleanup": receipt.public_payload()}
    if error.failure_diagnostic is not None:
        value["failure"] = error.failure_diagnostic
    return _public_diagnostics(value)


def sync_account_data(
    account_id: int,
    report: Callable[[dict], None] | None = None,
    *,
    validation_mode: bool = False,
    visible_readback: Callable[[object, CollectionBatch], object] | None = None,
) -> dict:
    if type(validation_mode) is not bool:
        raise CollectionFailure("metric_payload_invalid")
    account = _account(account_id)
    platform_type = account.get("type")
    if type(platform_type) is not int:
        raise CollectionFailure("collector_not_available")
    if validation_mode and platform_type != 1:
        raise CollectionFailure("collector_not_available")
    if validation_mode and not callable(visible_readback):
        raise CollectionFailure("metric_payload_invalid")
    source_mode = "direct_session"
    diagnostics = None
    try:
        try:
            collector = collector_for_platform(
                platform_type,
                **({"visible_readback": visible_readback} if validation_mode else {}),
            )
        except CollectionFailure as exc:
            _report_stage(report, "failed")
            saved = platform_data_service.record_failed_sync(
                account_id,
                platform_type,
                source_mode,
                _public_error_code(exc.error_code),
            )
            return _public_result(
                saved,
                account_id=account_id,
                source_mode=source_mode,
                metric_count=0,
                content_count=0,
            )
        _report_stage(report, "direct_session")
        try:
            batch = collector.collect_direct(account)
        except PlatformDataCollectionError as direct_error:
            if not direct_error.fallback_allowed:
                raise
            source_mode = "browser_signed"
            _report_stage(report, "browser_signed")
            detailed_collect = getattr(
                type(collector), "collect_browser_signed_with_diagnostics", None
            )
            if callable(detailed_collect):
                outcome = detailed_collect(
                    collector, account, validation_mode=validation_mode
                )
                batch = getattr(outcome, "batch", None)
                diagnostics = _public_diagnostics(
                    outcome.public_diagnostics()
                    if callable(getattr(outcome, "public_diagnostics", None))
                    else None
                )
                if type(batch) is not CollectionBatch or diagnostics is None:
                    raise CollectionFailure("metric_payload_invalid")
                if validation_mode and "validation" not in diagnostics:
                    raise CollectionFailure("metric_payload_invalid")
            else:
                batch = collector.collect_browser_signed(account)
        _report_stage(report, "account_metrics")
        _report_stage(report, "content_list")
        if batch.content_data_available:
            _report_stage(report, "content_metrics")
        _report_stage(report, "persisting")
        saved = (
            platform_data_service.record_validated_collection_sync(
                account_id,
                batch,
                diagnostics["validation"]["officialVisible"],
            )
            if validation_mode and diagnostics is not None and "validation" in diagnostics
            else platform_data_service.record_collection_sync(account_id, batch)
        )
        atomic_validation = None
        if validation_mode:
            if diagnostics is None or "validation" not in diagnostics:
                raise CollectionFailure("metric_payload_invalid")
            atomic_validation = _atomic_validation_result(
                saved.get("validation"),
                diagnostics["validation"]["officialVisible"],
            )
            if atomic_validation is None:
                raise CollectionFailure("validation_readback_mismatch")
        result = _public_result(
            saved,
            account_id=account_id,
            source_mode=source_mode,
            metric_count=len(batch.metrics),
            content_count=len(batch.contents),
            diagnostics=diagnostics,
        )
        if validation_mode:
            result["diagnostics"] = {
                "cleanup": diagnostics["cleanup"],
                "validation": {
                    "mode": "official_visible_readback",
                    **atomic_validation,
                },
            }
        _report_stage(report, _FINAL_STAGE_BY_STATUS[result["status"]])
        return result
    except PlatformDataCollectionError as exc:
        _report_stage(report, "failed")
        saved = platform_data_service.record_failed_sync(
            account_id,
            platform_type,
            source_mode,
            _public_error_code(exc.error_code),
        )
        return _public_result(
            saved,
            account_id=account_id,
            source_mode=source_mode,
            metric_count=0,
            content_count=0,
            diagnostics=_error_diagnostics(exc),
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except CollectionFailure as exc:
        if exc.error_code != "validation_readback_mismatch":
            raise
        _report_stage(report, "failed")
        saved = platform_data_service.record_failed_sync(
            account_id,
            platform_type,
            source_mode,
            "validation_readback_mismatch",
        )
        return _public_result(
            saved,
            account_id=account_id,
            source_mode=source_mode,
            metric_count=0,
            content_count=0,
            diagnostics=(
                {"cleanup": diagnostics["cleanup"]}
                if type(diagnostics) is dict and "cleanup" in diagnostics
                else None
            ),
        )
    except BaseException:
        _report_stage(report, "failed")
        saved = platform_data_service.record_failed_sync(
            account_id,
            platform_type,
            source_mode,
            "metric_payload_invalid",
        )
        return _public_result(
            saved,
            account_id=account_id,
            source_mode=source_mode,
            metric_count=0,
            content_count=0,
        )
