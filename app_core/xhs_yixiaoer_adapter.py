# -*- coding: utf-8 -*-
"""小红书旧模块名兼容层。

小红书运行能力已经迁入 :mod:`app_core.xhs_native_adapter`。保留本文件只为兼容
此前生成的任务和测试导入；它不会启动或调用蚁小二客户端、yxer、网关或签名服务。
新代码不得再依赖本模块名。
"""

from .xhs_native_adapter import (
    XhsNativeAdapter,
    XhsNativeAdapterError,
    build_native_contract,
)


# 兼容旧调用名称，下一次稳定版本迁移后可移除。
XhsYixiaoerAdapter = XhsNativeAdapter
XhsYixiaoerAdapterError = XhsNativeAdapterError
build_yixiaoer_contract = build_native_contract


__all__ = [
    "XhsYixiaoerAdapter",
    "XhsYixiaoerAdapterError",
    "build_yixiaoer_contract",
]
