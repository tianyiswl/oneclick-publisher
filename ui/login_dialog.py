# -*- coding: utf-8 -*-
"""账号登录弹窗。"""

from __future__ import annotations

import base64
import urllib.request

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QColor, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
)

from app_core import account_service, login_service

from .common import button


QR_CANVAS_SIZE = 248
QR_CONTENT_MAX_SIZE = 220


def _qr_display_size(width: int, height: int) -> tuple[int, int]:
    """保留小尺寸二维码的原始像素，只缩小过大图片。"""

    if width <= 0 or height <= 0:
        return 0, 0
    if max(width, height) <= QR_CONTENT_MAX_SIZE:
        return width, height
    scale = QR_CONTENT_MAX_SIZE / max(width, height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def _qr_display_pixmap(source: QPixmap) -> QPixmap:
    """将二维码置于固定白色画布中，避免放大模糊和窗口裁切。"""

    width, height = _qr_display_size(source.width(), source.height())
    if width <= 0 or height <= 0:
        return QPixmap()
    content = source
    if (width, height) != (source.width(), source.height()):
        content = source.scaled(
            width,
            height,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
    canvas = QPixmap(QR_CANVAS_SIZE, QR_CANVAS_SIZE)
    canvas.fill(QColor("white"))
    painter = QPainter(canvas)
    painter.drawPixmap(
        (QR_CANVAS_SIZE - content.width()) // 2,
        (QR_CANVAS_SIZE - content.height()) // 2,
        content,
    )
    painter.end()
    return canvas


class LoginDialog(QDialog):
    def __init__(self, parent=None, account: dict | None = None, background_login: bool = True) -> None:
        super().__init__(parent)
        self.setWindowTitle("一键发账号登录")
        self.resize(680, 680)
        self.session = None
        self.success = False
        self.account = account
        self.background_login = bool(background_login)
        self.scan_notified = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)

        heading = QLabel("一键发账号登录")
        heading.setObjectName("dialogTitle")
        layout.addWidget(heading)

        form_panel = QFrame()
        form_panel.setProperty("subPanel", True)
        form_panel_layout = QVBoxLayout(form_panel)
        form_panel_layout.setContentsMargins(14, 12, 14, 12)
        form_panel_layout.setSpacing(8)
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(9)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.platform_combo = QComboBox()
        self.platform_combo.setMinimumWidth(360)
        self.platform_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        for platform_type, platform_name in account_service.LOGIN_PLATFORM_OPTIONS:
            self.platform_combo.addItem(platform_name, platform_type)
        self.profile_input = QComboBox()
        self.profile_input.setEditable(True)
        self.profile_input.setMinimumWidth(360)
        self.profile_input.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        for profile in account_service.list_profiles():
            self.profile_input.addItem(profile)
        if account:
            login_type = account_service.login_platform_type(account["type"])
            self.platform_combo.setCurrentIndex(self.platform_combo.findData(login_type))
            self.platform_combo.setEnabled(False)
            self.profile_input.setCurrentText(account["profileName"])
        form.addRow("平台", self.platform_combo)
        form.addRow("主体", self.profile_input)
        form_panel_layout.addLayout(form)
        layout.addWidget(form_panel)

        self.qr_label = QLabel()
        self.qr_label.setObjectName("loginQrPanel")
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_label.setMinimumHeight(286)
        layout.addWidget(self.qr_label, 1)

        self.log = QTextEdit()
        self.log.setObjectName("loginStatusLog")
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(112)
        self.log.setMaximumHeight(150)
        self.log.setPlaceholderText("登录进度和异常提示会显示在这里")
        layout.addWidget(self.log)

        actions = QHBoxLayout()
        self.start_btn = button("开始登录", variant="primary")
        self.start_btn.clicked.connect(self.start_login)
        self.save_btn = button("手动保存（兜底）", variant="secondary")
        self.save_btn.setEnabled(False)
        self.save_btn.setToolTip("一键发通常会自动保存；仅在平台未返回可识别身份回执时作为兜底。")
        self.save_btn.clicked.connect(self.save_logged_in_account)
        self.cancel_btn = button("取消登录", variant="warning")
        self.cancel_btn.clicked.connect(self.cancel_login)
        close_btn = button("关闭", variant="secondary")
        close_btn.clicked.connect(self.close)
        actions.addWidget(close_btn)
        actions.addStretch()
        actions.addWidget(self.cancel_btn)
        actions.addWidget(self.save_btn)
        actions.addWidget(self.start_btn)
        layout.addLayout(actions)

        self.timer = QTimer(self)
        self.timer.setInterval(350)
        self.timer.timeout.connect(self.poll_messages)
        self.platform_combo.currentIndexChanged.connect(self.reset_login_prompt)
        self.reset_login_prompt()

    def reset_login_prompt(self, *_args) -> None:
        if self.session and self.timer.isActive():
            return
        self.qr_label.clear()
        self.qr_label.setText("请点击下方“开始登录”在一键发独立会话中打开官方页面")

    def start_login(self) -> None:
        profile = self.profile_input.currentText().strip()
        if not profile:
            QMessageBox.warning(self, "账号登录", "请输入主体名称")
            return
        platform_type = int(self.platform_combo.currentData())
        background_login = self.background_login
        self.log.clear()
        self.qr_label.clear()
        if background_login:
            self.log.append("一键发账号授权始终使用可见官方页面，已忽略后台运行设置。")
        self.qr_label.setText("正在打开平台官方登录页面...")
        self.start_btn.setEnabled(False)
        self.save_btn.setEnabled(False)
        self.scan_notified = False
        self.success = False
        self.session = login_service.start_login(
            platform_type,
            profile,
            update_mode=bool(self.account),
            record_id=self.account["id"] if self.account else None,
            background_mode=background_login,
        )
        self.timer.start()

    def save_logged_in_account(self) -> None:
        if not self.session:
            return
        self.save_btn.setEnabled(False)
        self.qr_label.setText("正在保存一键发本地登录会话...")
        self.log.append("正在保存本地会话引用，并标记为登录正常。")
        self.session.save()

    def cancel_login(self) -> None:
        if self.session:
            self.session.cancel()
        self.save_btn.setEnabled(False)
        self.log.append("登录流程已取消")

    def poll_messages(self) -> None:
        if not self.session:
            return
        while not self.session.queue.empty():
            msg = str(self.session.queue.get())
            if msg == "SCAN_CONFIRMED":
                self.log.append("扫码成功，正在验证并保存账号数据，请稍等...")
                self.qr_label.setText("扫码成功，正在验证并保存账号数据...")
                self.scan_notified = True
                continue
            if msg == "BROWSER_OPENED":
                self.qr_label.setText("官方登录页面已打开，完成登录后将自动保存账号")
                self.log.append("官方登录页面已由一键发打开。")
                self.save_btn.setEnabled(True)
                continue
            if msg == "LOGIN_DETECTED":
                self.save_btn.setEnabled(False)
                self.qr_label.setText("已检测到平台登录，正在自动保存账号信息...")
                self.log.append("已检测到平台身份回执，正在自动保存一键发本地会话。")
                continue
            if msg.startswith("ACCOUNT_SAVED:"):
                self.success = True
                self.timer.stop()
                self.log.append("一键发本地登录会话已自动保存，登录状态正常。")
                self.qr_label.setText("账号会话已自动保存，登录状态正常")
                QMessageBox.information(
                    self,
                    "一键发账号登录",
                    "账号会话已自动保存，登录状态正常。发布资格仍由任务预检单独判断。",
                )
                self.accept()
                return
            if msg.startswith("ACCOUNT_ID:"):
                self.log.append("扫码成功，正在保存登录数据...")
                self.qr_label.setText("扫码成功，正在保存账号数据...")
                if not self.scan_notified:
                    self.scan_notified = True
                    QMessageBox.information(self, "账号登录", "扫码成功，正在保存账号数据，请稍等。")
                continue
            if msg == "200":
                self.success = True
                self.timer.stop()
                self.log.append("登录成功，账号数据已保存。")
                QMessageBox.information(self, "账号登录", "登录成功，账号数据已保存。")
                self.accept()
                return
            if msg in ("500", "CANCELLED"):
                self.timer.stop()
                if msg == "CANCELLED":
                    self.log.append("登录已取消。")
                    self.qr_label.clear()
                    self.qr_label.setText("登录已取消，请点击“开始登录”重试")
                else:
                    self.log.append("登录失败。")
                    self.qr_label.clear()
                    self.qr_label.setText("登录失败，请检查提示后点击“开始登录”重试")
                self.start_btn.setEnabled(True)
                self.save_btn.setEnabled(False)
                return
            if msg.startswith("ERROR:"):
                self.log.append(msg.replace("ERROR:", "错误：", 1))
                self.timer.stop()
                self.start_btn.setEnabled(True)
                self.save_btn.setEnabled(False)
                self.qr_label.setText("未能打开官方登录页，请查看错误提示后重试")
                continue
            if msg.startswith("http") or msg.startswith("data:image"):
                self.show_qr(msg)
                self.log.append("二维码已获取，请扫码。")
            else:
                self.log.append(msg)

    def show_qr(self, src: str) -> None:
        try:
            if src.startswith("data:image"):
                raw = src.split(",", 1)[1]
                data = base64.b64decode(raw)
            else:
                with urllib.request.urlopen(src, timeout=15) as response:
                    data = response.read()
            pix = QPixmap()
            pix.loadFromData(data)
            if not pix.isNull():
                self.qr_label.setPixmap(_qr_display_pixmap(pix))
                return
        except Exception as exc:
            self.log.append(f"二维码显示失败：{exc}")
        self.qr_label.setText(src)

    def closeEvent(self, event) -> None:
        if self.session and self.timer.isActive() and not self.success:
            self.session.cancel()
            self.timer.stop()
        super().closeEvent(event)
