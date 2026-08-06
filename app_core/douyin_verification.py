# -*- coding: utf-8 -*-
"""抖音无头发布的本机内存验证协调器。

验证码和二维码只存在于当前进程内存。快照仅供原生客户端轮询，绝不包含
验证码、二维码字节、Cookie、手机号、原始页面内容或浏览器会话状态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
import math
import re
import threading
import time
from typing import Callable, Literal
import uuid

from PIL import Image, UnidentifiedImageError


MAX_QR_BYTES = 2 * 1024 * 1024
MIN_QR_SIDE = 120
TERMINAL_STATES = {"success", "failed", "cancelled", "expired"}
ACTIVE_STATES = {"waiting", "processing"}


class DouyinVerificationError(RuntimeError):
    """抖音验证无法安全继续。"""


@dataclass(frozen=True)
class VerificationChallenge:
    """执行器识别出的可受控验证挑战。

    二维码仍只以进程内字节传递；其 ``repr`` 不会显示图像内容，调用方也
    不应将实例写入事件、日志或持久化介质。
    """

    kind: Literal["sms", "qr"]
    message: str
    qr_image: bytes = field(default=b"", repr=False, compare=False)


@dataclass
class VerificationRequest:
    request_id: str
    task_id: int
    kind: Literal["sms", "qr"]
    message: str
    expires_at: float
    qr_image: bytes = field(default=b"", repr=False)
    pending_code: str = field(default="", repr=False)
    state: str = "waiting"
    condition: threading.Condition = field(
        default_factory=threading.Condition,
        repr=False,
    )


def _validate_qr_image(qr_image: bytes) -> bytes:
    """验证二维码图像可在内存解码，但不读取或输出其业务内容。"""

    if not isinstance(qr_image, bytes):
        raise DouyinVerificationError("抖音验证二维码必须为字节数据")
    if not qr_image:
        raise DouyinVerificationError("抖音验证二维码为空")
    if len(qr_image) > MAX_QR_BYTES:
        raise DouyinVerificationError("抖音验证二维码超过本机内存展示上限")
    try:
        with Image.open(BytesIO(qr_image)) as image:
            image.load()
            width, height = image.size
            if min(width, height) < MIN_QR_SIDE:
                raise DouyinVerificationError("抖音验证二维码像素尺寸过小")
            ratio = width / max(1, height)
            if not 0.72 <= ratio <= 1.38:
                raise DouyinVerificationError("抖音验证二维码画面不是近似方形")

            grayscale = image.convert("L")
            if max(grayscale.size) > 480:
                grayscale.thumbnail((480, 480))
            dark = grayscale.point(lambda pixel: 255 if pixel < 150 else 0)
            bbox = dark.getbbox()
            if not bbox:
                raise DouyinVerificationError("抖音验证二维码图像为空白")
            left, top, right, bottom = bbox
            coverage_x = (right - left) / max(1, grayscale.width)
            coverage_y = (bottom - top) / max(1, grayscale.height)
            dark_fraction = dark.histogram()[255] / max(
                1, grayscale.width * grayscale.height
            )
            if (
                coverage_x < 0.45
                or coverage_y < 0.45
                or not 0.08 <= dark_fraction <= 0.72
            ):
                raise DouyinVerificationError(
                    "当前图像只是空白区域或提示文字，不是可扫码二维码"
                )
    except DouyinVerificationError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise DouyinVerificationError("抖音验证二维码图像无法解码") from exc
    return bytes(qr_image)


def _snapshot_message(kind: Literal["sms", "qr"], state: str) -> str:
    """返回不依赖平台输入的固定安全说明。"""

    if state == "waiting":
        if kind == "sms":
            return "请在一键发客户端输入短信验证码"
        return "请在一键发客户端扫码完成验证"
    if state == "processing":
        return "正在提交验证码，请稍候"
    messages = {
        "success": "验证成功，发布会话将自动继续",
        "failed": "抖音验证失败，发布已安全停止",
        "cancelled": "用户已取消抖音验证，发布已安全停止",
        "expired": "抖音验证已过期，发布已安全停止",
    }
    return messages.get(state, "抖音验证状态异常，发布已安全停止")


class DouyinVerificationBroker:
    """在线程间传递验证状态，且不持久化任何敏感验证数据。"""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._requests: dict[str, VerificationRequest] = {}
        self._task_requests: dict[int, str] = {}

    def create_sms(
        self,
        *,
        task_id: int,
        message: str,
        expires_in_seconds: float = 600,
    ) -> str:
        return self._create(
            task_id=task_id,
            kind="sms",
            message=message,
            expires_in_seconds=expires_in_seconds,
        )

    def create_qr(
        self,
        *,
        task_id: int,
        qr_image: bytes,
        expires_in_seconds: float,
        message: str = "请使用抖音扫码完成验证",
    ) -> str:
        return self._create(
            task_id=task_id,
            kind="qr",
            message=message,
            expires_in_seconds=expires_in_seconds,
            qr_image=_validate_qr_image(qr_image),
        )

    def _create(
        self,
        *,
        task_id: int,
        kind: Literal["sms", "qr"],
        message: str,
        expires_in_seconds: float,
        qr_image: bytes = b"",
    ) -> str:
        try:
            normalized_task_id = int(task_id)
        except (TypeError, ValueError) as exc:
            raise DouyinVerificationError("抖音验证缺少有效任务号") from exc
        if normalized_task_id <= 0:
            raise DouyinVerificationError("抖音验证缺少有效任务号")
        try:
            expires_in_seconds = float(expires_in_seconds)
        except (TypeError, ValueError) as exc:
            raise DouyinVerificationError("抖音验证有效期必须为有限正数") from exc
        if not math.isfinite(expires_in_seconds) or expires_in_seconds <= 0:
            raise DouyinVerificationError("抖音验证有效期必须大于零")

        request = VerificationRequest(
            request_id=uuid.uuid4().hex,
            task_id=normalized_task_id,
            kind=kind,
            message=_snapshot_message(kind, "waiting"),
            expires_at=self._clock() + expires_in_seconds,
            qr_image=qr_image,
        )
        with self._lock:
            previous_id = self._task_requests.get(normalized_task_id)
            if previous_id:
                previous = self._requests.get(previous_id)
                if previous:
                    with previous.condition:
                        if self._state(previous) not in TERMINAL_STATES:
                            raise DouyinVerificationError("当前任务已有待处理的抖音验证")
            self._requests[request.request_id] = request
            self._task_requests[normalized_task_id] = request.request_id
        return request.request_id

    def _get(self, request_id: str) -> VerificationRequest:
        with self._lock:
            request = self._requests.get(str(request_id))
        if request is None:
            raise DouyinVerificationError("抖音验证请求不存在或已清理")
        return request

    def _state(self, request: VerificationRequest) -> str:
        if request.state in ACTIVE_STATES and self._clock() >= request.expires_at:
            request.state = "expired"
            request.pending_code = ""
            request.condition.notify_all()
        return request.state

    def snapshot(self, request_id: str) -> dict:
        request = self._get(request_id)
        with request.condition:
            state = self._state(request)
            return {
                "requestId": request.request_id,
                "taskId": request.task_id,
                "kind": request.kind,
                "state": state,
                "message": _snapshot_message(request.kind, state),
                "expiresInSeconds": max(0, round(request.expires_at - self._clock())),
                "hasQrImage": bool(request.qr_image),
            }

    def qr_image(self, request_id: str) -> bytes:
        """供原生二维码对话框从同一进程内存读取图像。"""

        request = self._get(request_id)
        with request.condition:
            if self._state(request) != "waiting" or request.kind != "qr":
                raise DouyinVerificationError("当前抖音验证没有可展示的二维码")
            return bytes(request.qr_image)

    def submit_code(self, request_id: str, code: str) -> None:
        value = str(code)
        if not re.fullmatch(r"\d{4,8}", value):
            raise DouyinVerificationError("验证码必须为 4 至 8 位数字")
        request = self._get(request_id)
        with request.condition:
            if self._state(request) != "waiting" or request.kind != "sms":
                raise DouyinVerificationError("当前抖音验证不能接收短信验证码")
            request.pending_code = value
            request.condition.notify_all()

    def consume_code(self, request_id: str) -> str | None:
        request = self._get(request_id)
        with request.condition:
            if self._state(request) != "waiting" or request.kind != "sms":
                return None
            code = request.pending_code
            request.pending_code = ""
            return code or None

    def claim_code(self, request_id: str) -> str | None:
        """原子领取待填短信码，并进入可被取消/过期检查的提交态。"""

        request = self._get(request_id)
        with request.condition:
            if self._state(request) != "waiting" or request.kind != "sms":
                raise DouyinVerificationError("当前抖音验证不能提交短信验证码")
            code = request.pending_code
            if not code:
                return None
            request.pending_code = ""
            request.state = "processing"
            request.condition.notify_all()
            return code

    def ensure_processing(self, request_id: str) -> None:
        """确认验证码写入前仍未被取消或超时。"""

        request = self._get(request_id)
        with request.condition:
            if self._state(request) != "processing":
                raise DouyinVerificationError("抖音验证码提交已取消或超时")

    def succeed(self, request_id: str) -> None:
        self._transition(request_id, "success", strict=True)

    def fail(
        self,
        request_id: str,
        message: str = "抖音验证失败，发布已安全停止",
    ) -> None:
        del message
        self._transition(request_id, "failed")

    def cancel(self, request_id: str) -> None:
        self._transition(request_id, "cancelled")

    def _transition(self, request_id: str, state: str, *, strict: bool = False) -> None:
        request = self._get(request_id)
        with request.condition:
            if self._state(request) in TERMINAL_STATES:
                if strict:
                    raise DouyinVerificationError("抖音验证已取消、失败或超时")
                return
            request.state = state
            request.message = _snapshot_message(request.kind, state)
            request.pending_code = ""
            request.condition.notify_all()

    def wait(self, request_id: str, *, timeout_seconds: float = 600) -> dict:
        """等待终态；等待超时一律安全停止，不会推断验证成功。"""

        if timeout_seconds <= 0:
            raise DouyinVerificationError("抖音验证等待时限必须大于零")
        request = self._get(request_id)
        deadline = self._clock() + float(timeout_seconds)
        with request.condition:
            while self._state(request) not in TERMINAL_STATES:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    request.state = "failed"
                    request.message = _snapshot_message(request.kind, "failed")
                    request.pending_code = ""
                    request.condition.notify_all()
                    break
                request.condition.wait(timeout=min(0.25, remaining))
            return self.snapshot(request_id)

    def clear(self, request_id: str) -> None:
        """从内存移除请求，并在移除前清空验证码和二维码。"""

        with self._lock:
            request = self._requests.pop(str(request_id), None)
            if request is None:
                return
            with request.condition:
                if request.state not in TERMINAL_STATES:
                    request.state = "cancelled"
                    request.message = _snapshot_message(request.kind, "cancelled")
                request.pending_code = ""
                request.qr_image = b""
                request.condition.notify_all()
            if self._task_requests.get(request.task_id) == request.request_id:
                self._task_requests.pop(request.task_id, None)


verification_broker = DouyinVerificationBroker()
