# -*- coding: utf-8 -*-
"""海外平台官方 OAuth/API 授权对话框。

界面初始化只读取本机脱敏状态，不访问平台。只有用户明确
点击“导入开发者配置”或“开始官方授权”才会修改本机私密配置
或打开平台授权页。
"""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QVBoxLayout,
)

from app_core import overseas_api_service

from .background_task import BackgroundTaskRunner
from .common import button


OFFICIAL_PLATFORM_OPTIONS = (
    (7, "YouTube"),
    (6, "TikTok"),
    (8, "Meta（Instagram / Facebook）"),
)


def _status_lines(status: dict) -> list[str]:
    configured = bool(
        status.get("clientConfigured") or status.get("brokerConfigured")
    )
    lines = [
        f"开发者配置：{'已导入' if configured else '未导入'}",
        f"官方授权：{'已存在' if status.get('tokenConfigured') else '未完成'}",
        f"访问状态：{'可用' if status.get('accessTokenValid') else '不可用或已过期'}",
        f"权限范围：{'完整' if status.get('scopeGranted') else '未验证'}",
    ]
    if status.get("displayName"):
        lines.append(f"已授权账号：{status['displayName']}")
    if status.get("pageCount") is not None:
        lines.append(
            f"Meta 资产：Facebook Page {int(status.get('pageCount') or 0)} 个、"
            f"Instagram 专业账号 {int(status.get('instagramAccountCount') or 0)} 个"
        )
    if status.get("expiresAt"):
        lines.append(f"令牌到期：{status['expiresAt']}")
    return lines


class OverseasAuthorizationDialog(QDialog):
    """导入项目自有开发者配置，并在一键发中完成官方授权。"""

    def __init__(
        self,
        parent=None,
        *,
        account: dict | None = None,
        initial_platform_type: int | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("海外平台官方 API 授权")
        self.resize(720, 560)
        self.account = dict(account or {})
        self.runner = BackgroundTaskRunner(self)
        self.success = False
        self.account_ids: list[int] = []
        self.lifecycle_message = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)

        heading = QLabel("海外平台官方 API 授权")
        heading.setObjectName("dialogTitle")
        layout.addWidget(heading)

        intro = QLabel(
            "这是一键发自有官方 OAuth/API 通道，不依赖蚁小二客户端。"
            "开发者配置和令牌只保存在本机私密目录，不写入数据库、"
            "Git、任务日志或发布包。"
        )
        intro.setObjectName("infoCallout")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        panel = QFrame()
        panel.setProperty("subPanel", True)
        form = QFormLayout(panel)
        form.setContentsMargins(14, 12, 14, 12)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        self.platform_combo = QComboBox()
        for platform_type, label in OFFICIAL_PLATFORM_OPTIONS:
            self.platform_combo.addItem(label, platform_type)
        self.profile_input = QLineEdit(
            str(self.account.get("profileName") or "").strip()
        )
        self.profile_input.setPlaceholderText("例如：墨白、Andy AI 实验室")
        form.addRow("平台", self.platform_combo)
        form.addRow("账号主体", self.profile_input)
        layout.addWidget(panel)

        requested = int(
            initial_platform_type
            or self.account.get("type")
            or OFFICIAL_PLATFORM_OPTIONS[0][0]
        )
        requested = 8 if requested == 9 else requested
        index = self.platform_combo.findData(requested)
        if index >= 0:
            self.platform_combo.setCurrentIndex(index)
        if self.account:
            self.platform_combo.setEnabled(False)

        self.status = QPlainTextEdit()
        self.status.setReadOnly(True)
        self.status.setMinimumHeight(150)
        layout.addWidget(self.status, 1)

        note = QLabel(
            "YouTube 需 Google Cloud 桌面 OAuth JSON；TikTok 需已配置回调的"
            " Content Posting API 应用；Meta 需 HTTPS OAuth Broker、Facebook Page 和"
            " Instagram 专业账号。TikTok 未通过平台审核前不会开放公开直发。"
        )
        note.setProperty("role", "muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        actions = QHBoxLayout()
        self.close_btn = button("关闭", variant="secondary")
        self.close_btn.clicked.connect(self.reject)
        self.refresh_btn = button("刷新状态", variant="secondary")
        self.refresh_btn.clicked.connect(self.refresh_status)
        self.import_btn = button("导入开发者配置", variant="secondary")
        self.import_btn.clicked.connect(self.import_config)
        self.authorize_btn = button("开始官方授权", variant="primary")
        self.authorize_btn.clicked.connect(self.authorize)
        actions.addWidget(self.close_btn)
        actions.addStretch()
        actions.addWidget(self.refresh_btn)
        actions.addWidget(self.import_btn)
        actions.addWidget(self.authorize_btn)
        layout.addLayout(actions)

        self.platform_combo.currentIndexChanged.connect(self.refresh_status)
        self.refresh_status()

    def platform_type(self) -> int:
        return int(self.platform_combo.currentData() or 0)

    def _set_running(self, running: bool) -> None:
        self.platform_combo.setEnabled(not running and not bool(self.account))
        self.profile_input.setEnabled(not running)
        self.refresh_btn.setEnabled(not running)
        self.import_btn.setEnabled(not running)
        self.authorize_btn.setEnabled(not running)
        self.close_btn.setEnabled(not running)

    def refresh_status(self) -> None:
        try:
            status = overseas_api_service.official_status(self.platform_type())
            self.status.setPlainText("\n".join(_status_lines(status)))
        except Exception as exc:
            self.status.setPlainText(f"无法读取本机授权状态：{exc}")

    def import_config(self) -> None:
        platform_type = self.platform_type()
        label = "JSON 配置 (*.json)"
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择开发者配置文件",
            "",
            label,
        )
        if not path:
            return
        try:
            result = overseas_api_service.configure_official_client(
                platform_type,
                path,
            )
        except Exception as exc:
            QMessageBox.warning(self, "导入开发者配置", str(exc))
            return
        self.lifecycle_message = f"{result.get('platform') or '平台'}开发者配置已安全导入本机。"
        self.refresh_status()
        QMessageBox.information(self, "导入开发者配置", self.lifecycle_message)

    def authorize(self) -> None:
        profile_name = self.profile_input.text().strip()
        if not profile_name:
            QMessageBox.warning(self, "官方授权", "请先填写账号主体。")
            return
        platform_type = self.platform_type()
        try:
            status = overseas_api_service.official_status(platform_type)
        except Exception as exc:
            QMessageBox.warning(self, "官方授权", str(exc))
            return
        if not (
            status.get("clientConfigured") or status.get("brokerConfigured")
        ):
            QMessageBox.warning(
                self,
                "官方授权",
                "请先导入当前平台的开发者配置。",
            )
            return

        self.status.setPlainText(
            "正在启动官方授权……\n"
            "平台可能在系统浏览器打开授权页；请只在官方域名下完成授权。"
        )

        def succeeded(payload: dict) -> None:
            self.account_ids = [
                int(item) for item in payload.get("accountIds") or [] if int(item) > 0
            ]
            if not self.account_ids:
                self.lifecycle_message = "官方授权已返回，但没有得到可用账号引用。"
                QMessageBox.warning(self, "官方授权", self.lifecycle_message)
                return
            evidence = [
                str(item.get("message") or "")
                for item in payload.get("evidence") or []
                if isinstance(item, dict) and item.get("message")
            ]
            self.lifecycle_message = (
                f"官方授权已完成，已保存 {len(self.account_ids)} 个可用发布目标。"
            )
            self.status.setPlainText(
                self.lifecycle_message
                + ("\n\n" + "\n".join(evidence) if evidence else "")
            )
            self.success = True
            QMessageBox.information(self, "官方授权", self.lifecycle_message)
            self.accept()

        self.runner.run(
            "official_authorization",
            lambda: overseas_api_service.authorize_official_account(
                platform_type,
                profile_name,
            ),
            on_started=lambda: self._set_running(True),
            on_success=succeeded,
            on_error=lambda message: QMessageBox.warning(
                self,
                "官方授权失败",
                message,
            ),
            on_finished=lambda: self._set_running(False),
        )
