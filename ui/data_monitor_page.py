# -*- coding: utf-8 -*-
"""平台数据监测页。"""

from __future__ import annotations

import time

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from app_core import account_service, platform_data_service, platform_data_sync

from .background_task import BackgroundTaskRunner
from .common import button


METRICS = (
    ("views", "播放"),
    ("likes", "点赞"),
    ("comments", "评论"),
    ("shares", "分享"),
    ("followers_total", "粉丝"),
    ("followers_net", "净增粉"),
    ("profile_visits", "主页访问"),
)

PROGRESS_TEXT = {
    "direct_session": "正在读取已登录账号数据…",
    "browser_signed": "正在读取抖音官方签名数据…",
    "persisting": "正在保存可信指标…",
    "completed": "数据同步完成",
    "failed": "数据同步未完成",
}

ERROR_TEXT = {
    "login_required": "需要重新登录",
    "verification_required": "需要完成平台验证",
    "metric_payload_empty": "平台暂未返回可用数据",
    "metric_payload_invalid": "平台数据暂时无法识别",
    "sync_persist_failed": "本地数据保存失败",
}


class DataMonitorPage(QWidget):
    request_account_management = pyqtSignal()

    def __init__(self, *, task_runner=None) -> None:
        super().__init__()
        self.setObjectName("pageRoot")
        self.runner = task_runner or BackgroundTaskRunner(self)
        self.metric_values: dict[str, QLabel] = {}
        self._shutting_down = False
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(16)

        header = QHBoxLayout()
        title = QLabel("数据监测")
        title.setObjectName("pageTitle")
        header.addWidget(title)
        header.addStretch()
        self.account_combo = QComboBox()
        self.account_combo.setMinimumWidth(240)
        self.account_combo.currentIndexChanged.connect(self._render_selected_account)
        header.addWidget(self.account_combo)
        self.sync_button = button("同步数据", variant="primary")
        self.sync_button.clicked.connect(self._start_sync)
        header.addWidget(self.sync_button)
        layout.addLayout(header)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        for column in range(4):
            grid.setColumnStretch(column, 1)
        for index, (key, label_text) in enumerate(METRICS):
            panel = QFrame()
            panel.setProperty("panel", True)
            card = QVBoxLayout(panel)
            card.setContentsMargins(16, 14, 16, 14)
            name = QLabel(label_text)
            name.setProperty("role", "muted")
            value = QLabel("—")
            value.setProperty("role", "metric")
            self.metric_values[key] = value
            card.addWidget(name)
            card.addWidget(value)
            grid.addWidget(panel, index // 4, index % 4)
        layout.addLayout(grid)

        status_panel = QFrame()
        status_panel.setProperty("panel", True)
        status_layout = QHBoxLayout(status_panel)
        status_layout.setContentsMargins(16, 14, 16, 14)
        self.status_label = QLabel("尚未同步")
        self.status_label.setWordWrap(True)
        status_layout.addWidget(self.status_label, 1)
        self.relogin_button = button("重新登录", variant="secondary")
        self.relogin_button.clicked.connect(self.request_account_management.emit)
        self.relogin_button.hide()
        status_layout.addWidget(self.relogin_button)
        layout.addWidget(status_panel)
        layout.addStretch(1)

    def refresh(self) -> None:
        selected = self.account_combo.currentData()
        accounts = [
            account
            for account in account_service.list_accounts()
            if type(account.get("type")) is int and account["type"] == 3
        ]
        self.account_combo.blockSignals(True)
        self.account_combo.clear()
        for account in accounts:
            label = account.get("profileName") or account.get("userName") or f"抖音账号 {account['id']}"
            self.account_combo.addItem(str(label), account["id"])
        if selected is not None:
            index = self.account_combo.findData(selected)
            if index >= 0:
                self.account_combo.setCurrentIndex(index)
        self.account_combo.blockSignals(False)
        self._render_selected_account()

    def _render_selected_account(self) -> None:
        account_id = self.account_combo.currentData()
        if type(account_id) is not int or account_id <= 0:
            for value in self.metric_values.values():
                value.setText("—")
            self.status_label.setText("请先在账号管理中添加抖音账号")
            self.relogin_button.hide()
            self.sync_button.setEnabled(False)
            return
        summary = platform_data_service.account_data_summary(account_id)
        metrics = summary.get("metrics") if isinstance(summary, dict) else {}
        if not isinstance(metrics, dict):
            metrics = {}
        for key, value_label in self.metric_values.items():
            value = metrics.get(key)
            value_label.setText("—" if value is None else f"{value:,}")
        latest = summary.get("latestRun") if isinstance(summary, dict) else None
        self.relogin_button.hide()
        if not isinstance(latest, dict):
            self.status_label.setText("尚未同步")
        elif latest.get("status") == "success":
            source = "官方签名读取" if latest.get("sourceMode") == "browser_signed" else "会话直连"
            self.status_label.setText(f"最近同步成功 · {source}")
        else:
            code = str(latest.get("errorCode") or "")
            self.status_label.setText(f"同步失败：{ERROR_TEXT.get(code, '数据同步未完成')}")
            if code == "login_required":
                self.relogin_button.show()
        key = f"platform-data-sync:{account_id}"
        is_running = getattr(self.runner, "is_running", None)
        if callable(is_running):
            active = bool(is_running(key))
        else:
            active = key in self.runner.active_keys_with_prefixes(("platform-data-sync",))
        self.sync_button.setEnabled(not self._shutting_down and not active)

    def _start_sync(self) -> None:
        if self._shutting_down:
            return
        account_id = self.account_combo.currentData()
        if type(account_id) is not int or account_id <= 0:
            return
        key = f"platform-data-sync:{account_id}"

        def started() -> None:
            if not self._shutting_down:
                self.sync_button.setEnabled(False)
                self.status_label.setText("正在同步数据…")

        def progressed(payload: object) -> None:
            if self._shutting_down or not isinstance(payload, dict):
                return
            text = PROGRESS_TEXT.get(payload.get("stage"))
            if text:
                self.status_label.setText(text)

        def completed(_result: object) -> None:
            if not self._shutting_down:
                self.refresh()

        def failed(_message: str) -> None:
            if not self._shutting_down:
                self.status_label.setText("数据同步未完成")

        def finished() -> None:
            if not self._shutting_down:
                self._render_selected_account()

        self.runner.run(
            key,
            with_progress=lambda report: platform_data_sync.sync_account_data(account_id, report=report),
            on_started=started,
            on_progress=progressed,
            on_success=completed,
            on_error=failed,
            on_finished=finished,
        )

    def shutdown(self) -> bool:
        self._shutting_down = True
        deadline = time.monotonic() + 5.0
        keys = self.runner.active_keys_with_prefixes(("platform-data-sync",))
        for key in keys:
            if self.runner.cancel_pending(key):
                continue
            remaining = max(0.0, deadline - time.monotonic())
            if not self.runner.wait_for_finished(key, remaining):
                return False
        return True
