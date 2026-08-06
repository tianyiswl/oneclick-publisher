# -*- coding: utf-8 -*-
"""抖音带货批量执行器的最终回执桥接。

完整的串行会话协调将在本模块继续扩展。当前入口只承担一个不能由旧运行时
替代的职责：把批量执行器从真实会话 ``submit`` 得到的、已完成最终平台回读
规范化后交给内部写入器。它不创建浏览器、不上传，也不执行发布。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ._douyin_commerce_batch_receipt_writer import _write_final_batch_receipt


_SHANGHAI_TIMEZONE = "Asia/Shanghai"


class DouyinCommerceBatchExecutorError(RuntimeError):
    """批量执行器未取得可写入成功状态的最终平台回读。"""


def write_verified_platform_result(
    task_id: int,
    item_id: int,
    result: Mapping[str, Any],
) -> None:
    """仅从批量执行器持有的最终会话结果写入一条成功回执。

    定时和立即发布都要求平台回读中明确给出 Asia/Shanghai 与完整日期时间。
    缺字段时抛错并保持任务非成功；调用方不得以本机当前时间补造回执。
    """

    if not isinstance(result, Mapping) or result.get("ok") is not True:
        raise DouyinCommerceBatchExecutorError("批量执行器缺少成功的平台最终结果")

    message = str(result.get("message") or "抖音平台最终回读完成").strip()
    if not message:
        raise DouyinCommerceBatchExecutorError("批量执行器缺少平台回执说明")

    if result.get("scheduled") is True:
        readback = result.get("scheduledReadback")
        if not isinstance(readback, Mapping):
            raise DouyinCommerceBatchExecutorError("抖音定时提交缺少平台管理页回读")
        schedule_time = readback.get("scheduledAt")
        timezone = readback.get("timezone")
        receipt = {
            "scheduleTime": schedule_time,
            "timezone": timezone,
        }
        event_type = "platform_scheduled_receipt"
    else:
        readback = result.get("platformReceipt")
        if not isinstance(readback, Mapping):
            raise DouyinCommerceBatchExecutorError("抖音发表缺少平台最终回执")
        timezone = readback.get("timezone")
        receipt = {
            key: readback.get(key)
            for key in ("platformPostId", "postUrl", "publishedAt", "timezone")
            if key in readback
        }
        event_type = "platform_publish_receipt"

    if timezone != _SHANGHAI_TIMEZONE:
        raise DouyinCommerceBatchExecutorError("抖音最终回执必须明确标注 Asia/Shanghai")
    try:
        _write_final_batch_receipt(
            int(task_id),
            int(item_id),
            event_type=event_type,
            readback=receipt,
            timezone=timezone,
            message=message,
        )
    except ValueError as exc:
        raise DouyinCommerceBatchExecutorError(str(exc)) from exc
