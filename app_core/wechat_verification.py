# -*- coding: utf-8 -*-
"""后台公众号发布的本机微信验证协调器。

二维码只保存在当前进程内存中。任务事件只记录状态，不记录图片、URL、
Cookie、令牌或其他凭据。发布线程等待同一个请求完成，扫码成功后即可从
原调用点继续同一浏览器会话。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
import threading
import time
import uuid
from typing import Callable

from PIL import Image, UnidentifiedImageError


MAX_QR_BYTES = 2 * 1024 * 1024
MIN_QR_SIDE = 120
TERMINAL_STATES = {"success", "cancelled", "failed"}
VISIBLE_STATES = {"waiting", "verifying", "expired", *TERMINAL_STATES}


class WechatVerificationError(RuntimeError):
    """微信验证无法安全继续。"""


RefreshCallback = Callable[[], bytes]
OpenPageCallback = Callable[[], None]
EventCallback = Callable[[str, str, str], None]


@dataclass
class VerificationRequest:
    request_id: str
    task_id: int
    qr_image: bytes
    expires_at: float
    refresh_callback: RefreshCallback | None = None
    open_page_callback: OpenPageCallback | None = None
    state: str = "waiting"
    message: str = "请使用微信扫码完成验证"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    condition: threading.Condition = field(
        default_factory=threading.Condition,
        repr=False,
    )


def validate_qr_image_bytes(value: bytes | bytearray | memoryview) -> bytes:
    """确认字节能解码为具有二维码像素特征的图像。

    这不是识别二维码内容，也不会把图像写入磁盘；只用于避免把空白容器、
    提示文字或损坏图片误当成可扫码二维码。
    """

    data = bytes(value)
    if not data:
        raise WechatVerificationError("微信验证二维码为空")
    if len(data) > MAX_QR_BYTES:
        raise WechatVerificationError("微信验证二维码超过本机内存展示上限")
    try:
        with Image.open(BytesIO(data)) as image:
            image.load()
            width, height = image.size
            if min(width, height) < MIN_QR_SIDE:
                raise WechatVerificationError("微信验证二维码像素尺寸过小")
            ratio = width / max(1, height)
            if not 0.72 <= ratio <= 1.38:
                raise WechatVerificationError("微信验证二维码画面不是近似方形")

            sample = image.convert("L")
            if max(sample.size) > 480:
                sample.thumbnail((480, 480))
            dark = sample.point(lambda pixel: 255 if pixel < 150 else 0)
            bbox = dark.getbbox()
            if not bbox:
                raise WechatVerificationError("微信验证二维码图像为空白")
            left, top, right, bottom = bbox
            bbox_width = right - left
            bbox_height = bottom - top
            coverage_x = bbox_width / max(1, sample.width)
            coverage_y = bbox_height / max(1, sample.height)
            histogram = dark.histogram()
            dark_fraction = histogram[255] / max(1, sample.width * sample.height)
            if (
                coverage_x < 0.45
                or coverage_y < 0.45
                or not 0.08 <= dark_fraction <= 0.72
            ):
                raise WechatVerificationError(
                    "当前图像只是空白区域或提示文字，不是可扫码二维码"
                )
    except WechatVerificationError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise WechatVerificationError("微信验证二维码图像无法解码") from exc
    return data


class WechatVerificationBroker:
    """在线程间传递二维码状态，不持久化二维码或登录凭据。"""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        event_callback: EventCallback | None = None,
    ) -> None:
        self._clock = clock
        self._event_callback = event_callback
        self._lock = threading.RLock()
        self._requests: dict[str, VerificationRequest] = {}
        self._task_requests: dict[int, str] = {}

    def _emit(self, request: VerificationRequest) -> None:
        callback = self._event_callback
        if callback:
            callback(request.request_id, request.state, request.message)

    def create(
        self,
        *,
        task_id: int,
        qr_image: bytes,
        expires_in_seconds: float = 120,
        refresh_callback: RefreshCallback | None = None,
        open_page_callback: OpenPageCallback | None = None,
    ) -> str:
        if int(task_id) <= 0:
            raise WechatVerificationError("微信验证缺少有效任务号")
        if expires_in_seconds <= 0:
            raise WechatVerificationError("微信验证二维码有效期必须大于零")
        request = VerificationRequest(
            request_id=uuid.uuid4().hex,
            task_id=int(task_id),
            qr_image=validate_qr_image_bytes(qr_image),
            expires_at=self._clock() + float(expires_in_seconds),
            refresh_callback=refresh_callback,
            open_page_callback=open_page_callback,
        )
        with self._lock:
            previous_id = self._task_requests.get(request.task_id)
            if previous_id:
                previous = self._requests.get(previous_id)
                if previous and previous.state not in TERMINAL_STATES:
                    raise WechatVerificationError("当前任务已有待处理的微信验证")
            self._requests[request.request_id] = request
            self._task_requests[request.task_id] = request.request_id
        self._emit(request)
        return request.request_id

    def request_for_task(self, task_id: int) -> str | None:
        with self._lock:
            return self._task_requests.get(int(task_id))

    def pending_task_ids(self) -> tuple[int, ...]:
        """返回仍需前台处理的任务号，不暴露二维码或请求内容。"""

        with self._lock:
            pending = [
                task_id
                for task_id, request_id in self._task_requests.items()
                if (request := self._requests.get(request_id)) is not None
                and request.state not in TERMINAL_STATES
            ]
        return tuple(sorted(pending))

    def _get(self, request_id: str) -> VerificationRequest:
        with self._lock:
            request = self._requests.get(str(request_id))
        if not request:
            raise WechatVerificationError("微信验证请求不存在或已清理")
        return request

    def snapshot(self, request_id: str) -> dict:
        request = self._get(request_id)
        with request.condition:
            if request.state in {"waiting", "verifying"} and self._clock() >= request.expires_at:
                request.state = "expired"
                request.message = "二维码已过期，请安全刷新"
                request.updated_at = time.time()
                request.condition.notify_all()
                self._emit(request)
            return {
                "requestId": request.request_id,
                "taskId": request.task_id,
                "state": request.state,
                "message": request.message,
                "expiresInSeconds": max(0, round(request.expires_at - self._clock())),
                "canRefresh": request.refresh_callback is not None,
                "canOpenPage": request.open_page_callback is not None,
                "hasQrImage": bool(request.qr_image),
            }

    def qr_image(self, request_id: str) -> bytes:
        request = self._get(request_id)
        with request.condition:
            return bytes(request.qr_image)

    def mark_verifying(self, request_id: str) -> None:
        self._transition(request_id, "verifying", "已扫码，正在验证")

    def succeed(self, request_id: str) -> None:
        self._transition(request_id, "success", "验证成功，发布会话将自动继续")

    def fail(self, request_id: str, message: str = "微信验证失败，发布已安全停止") -> None:
        self._transition(request_id, "failed", message)

    def cancel(self, request_id: str) -> None:
        self._transition(request_id, "cancelled", "用户已取消微信验证，发布已安全停止")

    def _transition(self, request_id: str, state: str, message: str) -> None:
        if state not in VISIBLE_STATES:
            raise WechatVerificationError("未知微信验证状态")
        request = self._get(request_id)
        with request.condition:
            if request.state in TERMINAL_STATES:
                return
            request.state = state
            request.message = str(message)
            request.updated_at = time.time()
            request.condition.notify_all()
        self._emit(request)

    def refresh(self, request_id: str, *, expires_in_seconds: float = 120) -> None:
        request = self._get(request_id)
        callback = request.refresh_callback
        if callback is None:
            self.fail(request_id, "二维码已过期且执行器不支持安全刷新")
            return
        try:
            qr_image = validate_qr_image_bytes(callback())
        except Exception as exc:
            self.fail(request_id, f"二维码刷新失败：{exc}")
            return
        with request.condition:
            if request.state in TERMINAL_STATES:
                return
            request.qr_image = qr_image
            request.expires_at = self._clock() + float(expires_in_seconds)
            request.state = "waiting"
            request.message = "二维码已刷新，请使用微信扫码"
            request.updated_at = time.time()
            request.condition.notify_all()
        self._emit(request)

    def open_page(self, request_id: str) -> None:
        request = self._get(request_id)
        callback = request.open_page_callback
        if callback is None:
            raise WechatVerificationError("当前执行器没有可打开的验证页面")
        callback()

    def wait(
        self,
        request_id: str,
        *,
        timeout_seconds: float = 600,
    ) -> dict:
        """等待 UI 完成验证；过期可刷新，整体超时则安全失败。"""

        request = self._get(request_id)
        deadline = self._clock() + float(timeout_seconds)
        with request.condition:
            while request.state not in TERMINAL_STATES:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    request.state = "failed"
                    request.message = "等待微信验证超时，发布已安全停止"
                    request.updated_at = time.time()
                    request.condition.notify_all()
                    self._emit(request)
                    break
                request.condition.wait(timeout=min(0.25, remaining))
            return self.snapshot(request_id)

    def clear(self, request_id: str) -> None:
        with self._lock:
            request = self._requests.pop(str(request_id), None)
            if request and self._task_requests.get(request.task_id) == request.request_id:
                self._task_requests.pop(request.task_id, None)


verification_broker = WechatVerificationBroker()


def request_wechat_verification(
    *,
    task_id: int,
    qr_image: bytes,
    expires_in_seconds: float = 120,
    refresh_callback: RefreshCallback | None = None,
    open_page_callback: OpenPageCallback | None = None,
    timeout_seconds: float = 600,
) -> dict:
    """供公众号发布执行器调用的阻塞式受控入口。

    返回 ``success`` 后执行器从当前调用点继续；取消、失败或超时会抛错，
    调用方不得新建浏览器上下文或绕过验证。
    """

    from . import task_service

    request_id = verification_broker.create(
        task_id=task_id,
        qr_image=qr_image,
        expires_in_seconds=expires_in_seconds,
        refresh_callback=refresh_callback,
        open_page_callback=open_page_callback,
    )
    task_service.record_task_event(
        task_id,
        "wechat_verification_required",
        "公众号发布需要微信验证，请在一键发客户端扫码",
        level="warning",
    )
    result = verification_broker.wait(request_id, timeout_seconds=timeout_seconds)
    if result["state"] != "success":
        task_service.record_task_event(
            task_id,
            "wechat_verification_stopped",
            result["message"],
            level="error",
        )
        raise WechatVerificationError(result["message"])
    task_service.record_task_event(
        task_id,
        "wechat_verification_succeeded",
        "微信验证成功，继续同一公众号发布会话",
    )
    verification_broker.clear(request_id)
    return result
