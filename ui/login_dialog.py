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
from .background_task import BackgroundTaskRunner


QR_CANVAS_SIZE = 248
QR_CONTENT_MAX_SIZE = 220
TIKTOK_LOGIN_ERROR_TEXT = {
    "tiktok_system_browser_unavailable": "未找到可用的系统 Chrome 或 Edge。",
    "tiktok_login_attempt_timeout": "等待登录超时，未保存账号。",
    "tiktok_login_profile_busy": "TikTok 临时登录资料仍被浏览器占用。",
    "tiktok_session_scope_invalid": "登录状态包含超出 TikTok 的数据，已拒绝保存。",
    "tiktok_session_missing": "未检测到可用的 TikTok 登录状态。",
    "tiktok_session_expired": "TikTok 登录状态已失效。",
    "tiktok_account_invalid": "TikTok 未返回唯一可核对账号。",
    "tiktok_account_identity_mismatch": "当前 TikTok 账号与原记录不一致。",
    "tiktok_login_cleanup_failed": "临时登录资料清理失败，已停止保存账号。",
    "tiktok_login_commit_failed": "TikTok 会话未能安全写入账号库。",
}


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
        self.lifecycle_message = ""
        self.readback_runner = BackgroundTaskRunner(self)
        self._saved_account_ids: list[int] = []

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
        platform_type = int(self.platform_combo.currentData() or 0)
        self.save_btn.setVisible(platform_type != 6)
        if platform_type == 6:
            self.save_btn.setEnabled(False)
            self.qr_label.setText(
                "请点击“开始登录”，一键发将打开系统 Chrome 专用临时窗口；"
                "登录完成后请关闭该窗口。"
            )
        elif platform_type == 7:
            self.qr_label.setText("请点击下方“开始登录”在系统默认浏览器中授权 YouTube")
        else:
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
            self.log.append("绑定账号需要你登录或扫码，即将打开可见官方页面。")
        self.qr_label.setText("正在打开平台官方登录页面...")
        self.start_btn.setEnabled(False)
        self.save_btn.setEnabled(False)
        self.scan_notified = False
        self.success = False
        self._saved_account_ids.clear()
        self.session = login_service.start_login(
            platform_type,
            profile,
            update_mode=bool(self.account),
            record_id=self.account["id"] if self.account else None,
            background_mode=background_login,
        )
        self.save_btn.setEnabled(
            bool(getattr(self.session, "manual_save_supported", True))
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
        self.lifecycle_message = "登录已取消，未修改账号会话。"
        self.log.append(self.lifecycle_message)
        self.timer.stop()
        self.reject()

    def _finish_success_after_readback(self, account_id: int) -> None:
        """仅在本地会话保存且账号静默回读正常后关闭登录窗口。"""

        self.success = True
        self.timer.stop()
        self.lifecycle_message = "登录成功：会话已保存，账号状态回读正常。"
        self.log.append(self.lifecycle_message)
        self.qr_label.setText("账号会话已保存，状态回读正常；正在返回账号管理")
        # 成功不再弹出需要用户确认的信息框，避免主窗口看似被自动关闭。
        QTimer.singleShot(0, self.accept)

    def _verify_saved_account(self, account_id: int) -> None:
        """把最终状态回读放到后台，避免扫码后冻结主窗口。"""

        self.qr_label.setText("会话已保存，正在静默回读账号状态...")
        self.log.append("正在回读一键发保存的账号状态。")

        def verify() -> dict:
            return account_service.validate_accounts([account_id])

        def verified(payload: dict) -> None:
            normal = {
                int(item.get("id"))
                for item in payload.get("normal", [])
                if item.get("id") is not None
            }
            if int(account_id) in normal:
                self._finish_success_after_readback(account_id)
                return
            self.lifecycle_message = "登录会话已保存，但账号状态回读未通过；请从账号管理重新登录。"
            self.log.append(self.lifecycle_message)
            self.timer.stop()
            self.reject()

        def verify_failed(message: str) -> None:
            self.lifecycle_message = f"登录会话已保存，但账号状态回读失败：{message}"
            self.log.append(self.lifecycle_message)
            self.timer.stop()
            self.reject()

        self.readback_runner.run(
            "verify_saved_account",
            verify,
            on_success=verified,
            on_error=verify_failed,
        )

    def poll_messages(self) -> None:
        if not self.session:
            return
        while not self.session.queue.empty():
            msg = str(self.session.queue.get())
            if msg == "OPENING_SYSTEM_BROWSER":
                self.save_btn.setEnabled(False)
                self.qr_label.setText("正在准备系统 Chrome 专用临时窗口...")
                self.log.append("正在创建本次 TikTok 登录使用的临时资料。")
                continue
            if msg == "SYSTEM_BROWSER_OPENED":
                self.save_btn.setEnabled(False)
                self.qr_label.setText("系统 Chrome 专用临时窗口已打开。")
                self.log.append("TikTok 官方登录页已在专用窗口打开。")
                continue
            if msg == "WAITING_BROWSER_EXIT":
                self.save_btn.setEnabled(False)
                self.qr_label.setText(
                    "请在系统 Chrome 专用临时窗口完成 TikTok 登录，"
                    "完成后关闭该窗口。"
                )
                self.log.append("等待你完成 TikTok 登录并关闭专用窗口。")
                continue
            if msg == "VALIDATING_TIKTOK_SESSION":
                self.save_btn.setEnabled(False)
                self.qr_label.setText("专用窗口已关闭，正在核对 TikTok 登录状态...")
                self.log.append("正在核对 TikTok 登录数据和账号身份。")
                continue
            if msg == "CLEANING_LOGIN_ATTEMPT":
                self.save_btn.setEnabled(False)
                self.qr_label.setText("正在清理 TikTok 临时登录资料...")
                self.log.append("正在清理本次 TikTok 登录使用的临时资料。")
                continue
            if msg == "SCAN_CONFIRMED":
                self.log.append("扫码成功，正在验证并保存账号数据，请稍等...")
                self.qr_label.setText("扫码成功，正在验证并保存账号数据...")
                self.scan_notified = True
                continue
            if msg == "BROWSER_OPENED":
                self.qr_label.setText("官方登录页面已打开，完成登录后将自动保存账号")
                self.log.append("官方登录页面已由一键发打开。")
                self.save_btn.setEnabled(
                    bool(getattr(self.session, "manual_save_supported", True))
                )
                continue
            if msg == "LOGIN_DETECTED":
                self.save_btn.setEnabled(False)
                self.qr_label.setText("已检测到平台登录，正在自动保存账号信息...")
                self.log.append("已检测到平台身份回执，正在自动保存一键发本地会话。")
                continue
            if msg.startswith("ACCOUNT_SAVED:"):
                self.timer.stop()
                account_id = int(msg.split(":", 1)[1])
                self._verify_saved_account(account_id)
                return
            if msg.startswith("ACCOUNT_ID:"):
                account_id = int(msg.split(":", 1)[1])
                if account_id not in self._saved_account_ids:
                    self._saved_account_ids.append(account_id)
                self.log.append("登录已校验，正在保存一键发账号数据...")
                self.qr_label.setText("登录已校验，正在保存账号数据...")
                if not self.scan_notified:
                    self.scan_notified = True
                continue
            if msg == "200":
                self.timer.stop()
                if self._saved_account_ids:
                    self._verify_saved_accounts(self._saved_account_ids)
                else:
                    self.lifecycle_message = "登录流程未返回可回读的账号标识，未保存账号。"
                    self.log.append(self.lifecycle_message)
                    self.reject()
                return
            if msg in ("500", "CANCELLED"):
                self.timer.stop()
                if msg == "CANCELLED":
                    self.lifecycle_message = "登录已取消，未修改账号会话。"
                else:
                    self.lifecycle_message = "登录失败，未修改账号会话。"
                self.log.append(self.lifecycle_message)
                self.reject()
                return
            if msg.startswith("ERROR:"):
                reason = msg.split(":", 1)[1]
                message = TIKTOK_LOGIN_ERROR_TEXT.get(reason)
                if message is None:
                    message = {
                        "youtube_oauth_client_not_configured": "尚未配置 Google 测试项目，当前不能开始 YouTube 官方登录。",
                        "authorization_denied": "你已拒绝 Google 授权，账号没有发生变化。",
                        "authorization_invalid": "Google 授权回调无效，请重新发起登录。",
                        "system_browser_open_failed": "系统默认浏览器未能打开 Google 授权页。",
                        "credential_unavailable": "系统凭据库不可用，未保存 YouTube 登录凭据。",
                        "channel_identity_mismatch": "本次授权频道与原账号不一致，未覆盖原账号。",
                        "channel_identity_unavailable": "Google 未返回唯一 YouTube 频道，未保存账号。",
                    }.get(reason, "YouTube 官方登录未完成，账号没有发生变化。")
                self.lifecycle_message = f"登录失败：{message}"
                self.log.append(self.lifecycle_message)
                self.timer.stop()
                self.reject()
                return
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

    def _verify_saved_accounts(self, account_ids: list[int]) -> None:
        """回读恢复流程保存的账号；Meta 会产生两个发布目标。"""

        expected = {int(item) for item in account_ids if int(item) > 0}
        self.qr_label.setText("会话已保存，正在静默回读海外平台状态...")

        def verify() -> dict:
            return account_service.validate_accounts(sorted(expected))

        def verified(payload: dict) -> None:
            normal = {
                int(item.get("id"))
                for item in payload.get("normal", [])
                if item.get("id") is not None
            }
            if expected and expected.issubset(normal):
                self._finish_success_after_readback(min(expected))
                return
            self.lifecycle_message = "账号会话已保存，但海外平台发布入口回读未全部通过。"
            self.log.append(self.lifecycle_message)
            self.reject()

        def verify_failed(message: str) -> None:
            self.lifecycle_message = f"海外账号回读失败：{message}"
            self.log.append(self.lifecycle_message)
            self.reject()

        self.readback_runner.run(
            "verify_saved_overseas_accounts",
            verify,
            on_success=verified,
            on_error=verify_failed,
        )

    def closeEvent(self, event) -> None:
        if self.session and self.timer.isActive() and not self.success:
            self.session.cancel()
            self.timer.stop()
        if not self.lifecycle_message and not self.success:
            self.lifecycle_message = "登录窗口已关闭，账号会话未变更。"
        super().closeEvent(event)
