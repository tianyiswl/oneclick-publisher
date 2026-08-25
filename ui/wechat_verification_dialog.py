# -*- coding: utf-8 -*-
"""后台公众号发布的原生微信验证对话框。"""

from __future__ import annotations

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QVBoxLayout,
)

from app_core.wechat_verification import (
    TERMINAL_STATES,
    WechatVerificationBroker,
    WechatVerificationError,
    verification_broker,
)

from .common import button
from .login_dialog import _qr_display_pixmap


class WechatVerificationDialog(QDialog):
    """显示内存中的实时二维码，不主动前置后台浏览器。"""

    def __init__(
        self,
        request_id: str,
        parent=None,
        *,
        broker: WechatVerificationBroker = verification_broker,
    ) -> None:
        super().__init__(parent)
        self.request_id = str(request_id)
        self.broker = broker
        self._last_image = b""
        self._image_load_failed = False
        self._terminal = False
        self.setWindowTitle("需要微信验证")
        self.setModal(True)
        self.resize(520, 590)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)

        heading = QLabel("需要微信验证")
        heading.setObjectName("dialogTitle")
        layout.addWidget(heading)

        description = QLabel(
            "公众号后台需要管理员或运营者使用微信扫码确认。"
            "验证成功后，一键发会继续当前任务。"
        )
        description.setWordWrap(True)
        description.setProperty("role", "muted")
        layout.addWidget(description)

        self.qr_label = QLabel("正在读取二维码…")
        self.qr_label.setObjectName("loginQrPanel")
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_label.setMinimumHeight(286)
        layout.addWidget(self.qr_label, 1)

        self.status_label = QLabel("等待二维码")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setWordWrap(True)
        self.status_label.setProperty("role", "countBadge")
        layout.addWidget(self.status_label)

        safety = QLabel(
            "二维码仅在本机内存中临时展示，不写入发布包、长期日志或外部服务。"
        )
        safety.setWordWrap(True)
        safety.setProperty("role", "caption")
        layout.addWidget(safety)

        actions = QHBoxLayout()
        self.open_page_btn = button("打开验证页面", variant="secondary")
        self.open_page_btn.setToolTip("备用入口：仅在你主动点击后显示执行器的验证页面")
        self.open_page_btn.clicked.connect(self.open_verification_page)
        self.refresh_btn = button("刷新二维码", variant="secondary")
        self.refresh_btn.clicked.connect(self.refresh_qr)
        self.cancel_btn = button("取消验证", variant="warning")
        self.cancel_btn.clicked.connect(self.cancel_verification)
        actions.addWidget(self.open_page_btn)
        actions.addStretch()
        actions.addWidget(self.refresh_btn)
        actions.addWidget(self.cancel_btn)
        layout.addLayout(actions)

        self.timer = QTimer(self)
        self.timer.setInterval(350)
        self.timer.timeout.connect(self.poll_state)
        self.timer.start()
        self.poll_state()

    def _show_qr(self, data: bytes) -> bool:
        pixmap = QPixmap()
        if not pixmap.loadFromData(data) or pixmap.isNull():
            self._image_load_failed = True
            self.qr_label.setText("二维码加载失败，请点击“刷新二维码”")
            return False
        self._image_load_failed = False
        display = _qr_display_pixmap(pixmap)
        if display.isNull():
            self._image_load_failed = True
            self.qr_label.setText("二维码绘制失败，请点击“刷新二维码”")
            return False
        self.qr_label.clear()
        self.qr_label.setPixmap(display)
        self.qr_label.setToolTip(
            f"二维码已从本机内存载入：{pixmap.width()} × {pixmap.height()} 像素"
        )
        self._last_image = bytes(data)
        return True

    def poll_state(self) -> None:
        try:
            snapshot = self.broker.snapshot(self.request_id)
            image = self.broker.qr_image(self.request_id)
        except WechatVerificationError as exc:
            self.timer.stop()
            self.status_label.setText(str(exc))
            self.status_label.setProperty("role", "warning")
            self._terminal = True
            return
        if image != self._last_image:
            self._show_qr(image)

        state = snapshot["state"]
        state_labels = {
            "waiting": "等待扫码",
            "verifying": "已扫码，正在验证",
            "expired": "二维码已过期",
            "success": "验证成功，正在继续当前任务",
            "cancelled": "验证已取消，当前任务已安全停止",
            "failed": "验证失败，当前任务已安全停止",
        }
        countdown = (
            f" · {snapshot['expiresInSeconds']} 秒后过期"
            if state in {"waiting", "verifying"} and snapshot["expiresInSeconds"]
            else ""
        )
        self.status_label.setText(
            f"{state_labels.get(state, snapshot['message'])}{countdown}\n{snapshot['message']}"
        )
        self.refresh_btn.setEnabled(
            (state == "expired" or self._image_load_failed)
            and bool(snapshot["canRefresh"])
        )
        self.open_page_btn.setEnabled(
            state not in TERMINAL_STATES and bool(snapshot["canOpenPage"])
        )
        self.cancel_btn.setEnabled(state not in TERMINAL_STATES)
        if state == "success":
            self._terminal = True
            self.timer.stop()
            QTimer.singleShot(500, self.accept)
        elif state in {"cancelled", "failed"}:
            self._terminal = True
            self.timer.stop()

    def refresh_qr(self) -> None:
        self.refresh_btn.setEnabled(False)
        self.status_label.setText("正在安全刷新二维码…")
        self.broker.refresh(self.request_id)
        self.poll_state()

    def open_verification_page(self) -> None:
        try:
            self.broker.open_page(self.request_id)
        except WechatVerificationError as exc:
            QMessageBox.warning(self, "打开验证页面", str(exc))

    def cancel_verification(self) -> None:
        self.broker.cancel(self.request_id)
        self._terminal = True
        self.timer.stop()
        self.reject()

    def reject(self) -> None:
        if not self._terminal:
            self.broker.cancel(self.request_id)
            self._terminal = True
        self.timer.stop()
        super().reject()
