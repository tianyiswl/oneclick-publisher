# -*- coding: utf-8 -*-
"""海外平台适配器必须实现的统一异步接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import (
    AuthorizationRequest,
    AuthorizationResult,
    CapabilityDescriptor,
    ReadbackResult,
    UploadRequest,
    UploadResult,
)


class OverseasPlatformCapability(ABC):
    """官方 API 或受控浏览器适配器的稳定入口。

    OAuth 客户配置和令牌存储由具体适配器注入，不能成为请求对象字段，也不能
    写入日志或 Git。所有网络和浏览器操作均使用异步方法，避免阻塞桌面主线程。
    """

    @property
    @abstractmethod
    def descriptor(self) -> CapabilityDescriptor:
        """返回平台、交付状态和已实现操作。"""

    @abstractmethod
    async def authorize(self, request: AuthorizationRequest) -> AuthorizationResult:
        """完成官方登录/授权，并分别返回登录与授权证据。"""

    @abstractmethod
    async def refresh_authorization(
        self,
        account_reference: str,
    ) -> AuthorizationResult:
        """刷新已有官方授权，不接收或返回原始刷新令牌。"""

    @abstractmethod
    async def upload(self, request: UploadRequest) -> UploadResult:
        """上传媒体并执行明确的草稿、私密、定时或公开目标。"""

    @abstractmethod
    async def readback(self, operation_reference: str) -> ReadbackResult:
        """回读平台结果，不能把本地预检或按钮点击当作平台成功。"""
