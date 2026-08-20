# -*- coding: utf-8 -*-
"""平台数据同步编排。"""

from __future__ import annotations

from collections.abc import Callable

from . import account_service, platform_data_service
from .douyin_data_collector import DouyinDataCollectionError
from .platform_data_collectors import collector_for_platform
from .platform_data_models import CollectionFailure


_PROGRESS_MESSAGES = {
    "direct_session": "正在读取已登录账号数据",
    "browser_signed": "正在读取抖音官方签名数据",
    "account_metrics": "正在整理账号核心指标",
    "content_list": "正在读取作品列表",
    "content_metrics": "正在整理作品指标",
    "persisting": "正在保存可信指标",
    "completed": "数据同步完成",
    "partial": "部分数据已保存",
    "failed": "数据同步未完成",
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
) -> dict:
    status = saved.get("status")
    if status == "success":
        error_code = ""
    elif status == "partial_success":
        error_code = _public_error_code(saved.get("errorCode"))
    else:
        status = "failed"
        error_code = _public_error_code(saved.get("errorCode"))
    return {
        "accountId": account_id,
        "status": status,
        "sourceMode": source_mode,
        "errorCode": error_code,
        "metricCount": metric_count,
        "contentCount": content_count,
    }


def sync_account_data(
    account_id: int,
    report: Callable[[dict], None] | None = None,
) -> dict:
    account = _account(account_id)
    platform_type = account.get("type")
    if type(platform_type) is not int:
        raise CollectionFailure("collector_not_available")
    collector = collector_for_platform(platform_type)
    source_mode = "direct_session"
    try:
        _report_stage(report, "direct_session")
        try:
            batch = collector.collect_direct(account)
        except DouyinDataCollectionError as direct_error:
            if not direct_error.fallback_allowed:
                raise
            source_mode = "browser_signed"
            _report_stage(report, "browser_signed")
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
        )
        _report_stage(
            report,
            "completed" if result["status"] == "success" else "partial",
        )
        return result
    except DouyinDataCollectionError as exc:
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
