# -*- coding: utf-8 -*-
"""平台数据同步编排。"""

from __future__ import annotations

from collections.abc import Callable

from . import account_service, platform_data_service
from .douyin_data_collector import DouyinDataCollectionError
from .platform_data_collectors import collector_for_platform
from .platform_data_models import CollectionFailure


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
        _emit(report, "direct_session", "正在读取已登录账号数据")
        try:
            batch = collector.collect_direct(account)
        except DouyinDataCollectionError as direct_error:
            if not direct_error.fallback_allowed:
                raise
            source_mode = "browser_signed"
            _emit(report, "browser_signed", "正在读取抖音官方签名数据")
            batch = collector.collect_browser_signed(account, report=report)
        _emit(report, "persisting", "正在保存可信指标")
        saved = platform_data_service.record_successful_sync(
            account_id,
            batch,
        )
        _emit(report, "completed", "数据同步完成")
        return saved
    except DouyinDataCollectionError as exc:
        _emit(report, "failed", "数据同步未完成")
        return platform_data_service.record_failed_sync(
            account_id,
            platform_type,
            source_mode,
            exc.error_code,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except CollectionFailure:
        raise
    except BaseException:
        _emit(report, "failed", "数据同步未完成")
        return platform_data_service.record_failed_sync(
            account_id,
            platform_type,
            source_mode,
            "metric_payload_invalid",
        )
