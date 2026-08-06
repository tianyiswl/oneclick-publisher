# -*- coding: utf-8 -*-
"""抖音无头提交的本机原生验证对话框。"""

from __future__ import annotations

from PyQt6.QtCore import QRegularExpression, QTimer, Qt
from PyQt6.QtGui import QPixmap, QRegularExpressionValidator
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout

from app_core.douyin_verification import (
    TERMINAL_STATES,
    DouyinVerificationBroker,
    DouyinVerificationError,
    verification_broker,
)

from .common import button


class DouyinVerificationDialog(QDialog):
    """仅展示 Broker 内存状态，不触碰或前置浏览器。"""

    def __init__(
        self,
        request_id: str,
        parent=None,
        *,
        broker: DouyinVerificationBroker = verification_broker,
    ) -> None:
        super().__init__(parent)
        self.request_id = str(request_id)
        self.broker = broker
        self._terminal = False
        self._kind = ""
        self.setWindowTitle("需要抖音验证")
        self.setModal(True)
        self.resize(440, 330)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)

        title = QLabel("需要抖音验证")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)

        self.description_label = QLabel()
        self.description_label.setWordWrap(True)
        self.description_label.setProperty("role", "muted")
        layout.addWidget(self.description_label)

        self.content_layout = QVBoxLayout()
        layout.addLayout(self.content_layout, 1)

        self.status_label = QLabel("正在读取验证状态…")
        self.status_label.setObjectName("douyinVerificationStatus")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setWordWrap(True)
        self.status_label.setProperty("role", "countBadge")
        layout.addWidget(self.status_label)

        safety = QLabel("验证码和二维码仅在本机内存中临时处理，不会写入任务记录或日志。")
        safety.setWordWrap(True)
        safety.setProperty("role", "caption")
        layout.addWidget(safety)

        actions = QHBoxLayout()
        actions.addStretch()
        self.cancel_button = button("取消验证", variant="warning")
        self.cancel_button.clicked.connect(self.cancel_verification)
        actions.addWidget(self.cancel_button)
        layout.addLayout(actions)

        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.poll_state)
        self.poll_state()
        self.timer.start()

    def _build_kind_content(self, kind: str) -> None:
        if kind == self._kind:
            return
        self._kind = kind
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if kind == "sms":
            self.description_label.setText("需要短信验证，请输入收到的数字验证码。")
            self.code_input = QLineEdit()
            self.code_input.setObjectName("douyinVerificationCode")
            self.code_input.setInputMethodHints(
                Qt.InputMethodHint.ImhDigitsOnly
            )
            self.code_input.setValidator(
                QRegularExpressionValidator(QRegularExpression(r"\d{0,8}"), self)
            )
            self.code_input.setMaxLength(8)
            self.code_input.setPlaceholderText("输入 4 至 8 位数字验证码")
            self.code_input.setEchoMode(QLineEdit.EchoMode.Password)
            self.code_input.returnPressed.connect(self.submit_code)
            self.content_layout.addWidget(self.code_input)
            self.submit_button = button("提交验证码", variant="primary")
            self.submit_button.clicked.connect(self.submit_code)
            self.content_layout.addWidget(self.submit_button)
        elif kind == "qr":
            self.description_label.setText("请扫码验证，验证完成后会自动继续提交。")
            self.qr_label = QLabel("正在从本机内存读取二维码…")
            self.qr_label.setObjectName("douyinVerificationQr")
            self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.qr_label.setMinimumHeight(220)
            self.content_layout.addWidget(self.qr_label)
        else:
            self.description_label.setText("抖音验证状态无法识别，提交已安全停止。")

    def _show_qr(self) -> None:
        if self._kind != "qr" or not hasattr(self, "qr_label"):
            return
        try:
            data = self.broker.qr_image(self.request_id)
        except DouyinVerificationError:
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(data) or pixmap.isNull():
            self.qr_label.setText("二维码无法在本机加载，提交已安全停止。")
            return
        self.qr_label.clear()
        self.qr_label.setPixmap(
            pixmap.scaled(
                220,
                220,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def poll_state(self) -> None:
        try:
            snapshot = self.broker.snapshot(self.request_id)
        except DouyinVerificationError:
            self.timer.stop()
            self._terminal = True
            self.status_label.setText("验证请求已结束，提交已安全停止。")
            self.cancel_button.setEnabled(False)
            return

        kind = str(snapshot.get("kind") or "")
        state = str(snapshot.get("state") or "")
        self._build_kind_content(kind)
        if kind == "qr" and state == "waiting":
            self._show_qr()

        state_labels = {
            "waiting": "需要短信验证" if kind == "sms" else "请扫码验证",
            "processing": "正在验证，无法取消",
            "success": "验证成功，正在继续提交。",
            "failed": "验证失败，提交已安全停止。",
            "cancelled": "验证已取消，提交已安全停止。",
            "expired": "验证已过期，提交已安全停止。",
        }
        self.status_label.setText(state_labels.get(state, "验证状态异常，提交已安全停止。"))
        processing = state == "processing"
        self.cancel_button.setEnabled(state not in TERMINAL_STATES and not processing)
        if kind == "sms" and hasattr(self, "submit_button"):
            self.submit_button.setEnabled(state == "waiting")
            self.code_input.setEnabled(state == "waiting")
        if state == "success":
            self._terminal = True
            self.timer.stop()
            QTimer.singleShot(250, self.accept)
        elif state in TERMINAL_STATES:
            self._terminal = True
            self.timer.stop()

    def submit_code(self) -> None:
        if not hasattr(self, "code_input"):
            return
        code = self.code_input.text()
        try:
            self.broker.submit_code(self.request_id, code)
        except DouyinVerificationError:
            self.status_label.setText("验证码格式无效，请输入 4 至 8 位数字。")
            return
        self.code_input.clear()
        self.status_label.setText("验证码已提交，正在验证。")

    def cancel_verification(self) -> None:
        try:
            cancelled = self.broker.cancel(self.request_id)
        except DouyinVerificationError:
            cancelled = False
        if not cancelled:
            self.status_label.setText("正在验证，无法取消")
            return
        self.poll_state()

    def closeEvent(self, event) -> None:
        if not self._terminal:
            try:
                snapshot = self.broker.snapshot(self.request_id)
                if snapshot.get("state") == "waiting":
                    self.broker.cancel(self.request_id)
                    self._terminal = True
                    self.timer.stop()
                elif snapshot.get("state") == "processing":
                    self.status_label.setText("正在验证，无法取消")
                    event.ignore()
                    return
            except DouyinVerificationError:
                self._terminal = True
                self.timer.stop()
        event.accept()
