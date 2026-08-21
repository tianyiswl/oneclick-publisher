# -*- coding: utf-8 -*-
"""平台数据同步编排。"""

from __future__ import annotations

from collections.abc import Callable

from . import account_service, platform_data_service
from .platform_data_collection_errors import PlatformDataCollectionError
from .platform_data_collectors import collector_for_platform
from .platform_data_models import CollectionBatch, CollectionFailure


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
    if type(value) is not dict or set(value) not in ({"cleanup"}, {"cleanup", "validation"}):
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


def _validation_readback(account_id: int, diagnostics: dict) -> dict:
    official = diagnostics["validation"]["officialVisible"]
    followers = official["account"]["followersTotal"]
    content_id = official["content"]["contentId"]
    views = official["content"]["views"]
    summary = platform_data_service.account_data_summary(account_id)
    contents = platform_data_service.account_contents(account_id, limit=1, offset=0)
    summary_metrics = summary.get("metrics") if type(summary) is dict else None
    local_followers = (
        summary_metrics.get("followers_total")
        if type(summary_metrics) is dict
        and type(summary_metrics.get("followers_total")) in (int, float)
        else None
    )
    local_content = None
    items = contents.get("items") if type(contents) is dict else None
    if type(items) is list:
        for item in items:
            if type(item) is not dict or item.get("contentId") != content_id:
                continue
            metrics = item.get("metrics")
            local_views = (
                metrics.get("views")
                if type(metrics) is dict and type(metrics.get("views")) in (int, float)
                else None
            )
            local_content = {"contentId": content_id, "views": local_views}
            break
    if local_content is None:
        local_content = {"contentId": content_id, "views": None}
    return {
        "mode": "official_visible_readback",
        "officialVisible": official,
        "localReadback": {
            "account": {"followersTotal": local_followers},
            "content": local_content,
        },
        "matches": {
            "followersTotal": local_followers == followers,
            "views": local_content["views"] == views,
        },
    }


def _error_diagnostics(error: PlatformDataCollectionError) -> dict | None:
    receipt = error.cleanup_receipt
    return (
        _public_diagnostics({"cleanup": receipt.public_payload()})
        if receipt is not None
        else None
    )


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
        saved = platform_data_service.record_collection_sync(
            account_id,
            batch,
        )
        result = _public_result(
            saved,
            account_id=account_id,
            source_mode=source_mode,
            metric_count=len(batch.metrics),
            content_count=len(batch.contents),
            diagnostics=diagnostics,
        )
        if validation_mode:
            if diagnostics is None or "validation" not in diagnostics:
                raise CollectionFailure("metric_payload_invalid")
            result["diagnostics"] = {
                "cleanup": diagnostics["cleanup"],
                "validation": _validation_readback(account_id, diagnostics),
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
    except CollectionFailure:
        raise
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
