# -*- coding: utf-8 -*-
"""首页仪表盘页面。"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from app_core import account_service, activation_service, media_service, task_service

from .common import COLORS, PLATFORM_COLORS, button, table_item


TASK_STATUS = {
    "pending": "等待执行",
    "running": "执行中",
    "success": "成功",
    "partial_failed": "部分失败",
    "failed": "失败",
    "cancelled": "已取消",
}

TASK_STATUS_COLORS = {
    "pending": COLORS["muted"],
    "running": COLORS["info"],
    "success": COLORS["success"],
    "partial_failed": COLORS["warning"],
    "failed": COLORS["danger"],
    "cancelled": COLORS["muted"],
}


class DashboardPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("pageRoot")
        self.cards: dict[str, QLabel] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(16)

        header = QHBoxLayout()
        title = QLabel("工作台")
        title.setObjectName("pageTitle")
        header.addWidget(title)
        header.addStretch()
        self.license_label = QLabel("授权状态")
        self.license_label.setProperty("role", "countBadge")
        header.addWidget(self.license_label)
        refresh = button("刷新", variant="secondary")
        refresh.clicked.connect(self.refresh)
        header.addWidget(refresh)
        layout.addLayout(header)

        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(12)
        self.grid.setVerticalSpacing(12)
        for column in range(4):
            self.grid.setColumnStretch(column, 1)
        layout.addLayout(self.grid)

        lower = QHBoxLayout()
        lower.setSpacing(12)
        lower.addWidget(self._platform_panel(), 4)
        lower.addWidget(self._task_panel(), 6)
        layout.addLayout(lower, 1)

        self.refresh()

    def _platform_panel(self) -> QFrame:
        panel = QFrame()
        panel.setProperty("panel", True)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 16)
        layout.setSpacing(10)

        title = QLabel("账号平台状态")
        title.setProperty("role", "sectionTitle")
        layout.addWidget(title)

        self.platform_table = QTableWidget(0, 4)
        self.platform_table.setHorizontalHeaderLabels(["平台", "账号", "正常", "异常"])
        self.platform_table.verticalHeader().setVisible(False)
        self.platform_table.setShowGrid(False)
        self.platform_table.setAlternatingRowColors(True)
        self.platform_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.platform_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self.platform_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.platform_table)
        return panel

    def _task_panel(self) -> QFrame:
        panel = QFrame()
        panel.setProperty("panel", True)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 16)
        layout.setSpacing(10)

        title_row = QHBoxLayout()
        title = QLabel("最近发布任务")
        title.setProperty("role", "sectionTitle")
        title_row.addWidget(title)
        title_row.addStretch()
        self.latest_count = QLabel("0 条")
        self.latest_count.setProperty("role", "muted")
        title_row.addWidget(self.latest_count)
        layout.addLayout(title_row)

        self.task_table = QTableWidget(0, 4)
        self.task_table.setHorizontalHeaderLabels(["任务", "平台", "状态", "创建时间"])
        self.task_table.verticalHeader().setVisible(False)
        self.task_table.setShowGrid(False)
        self.task_table.setAlternatingRowColors(True)
        self.task_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.task_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self.task_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.task_table)
        return panel

    def _card(
        self,
        row: int,
        col: int,
        title: str,
        value: str,
        detail: str = "",
        *,
        tone: str = "primary",
    ) -> None:
        card = QFrame()
        card.setObjectName("metricCard")
        card.setProperty("metricTone", tone)
        card.setMinimumHeight(108)
        inner = QVBoxLayout(card)
        inner.setContentsMargins(17, 14, 17, 14)
        inner.setSpacing(5)

        caption = QLabel(title)
        caption.setProperty("role", "caption")
        value_label = QLabel(value)
        value_label.setObjectName("metricValue")
        value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        inner.addWidget(caption)
        inner.addWidget(value_label)
        if detail:
            detail_label = QLabel(detail)
            detail_label.setProperty("role", "muted")
            inner.addWidget(detail_label)
        else:
            inner.addStretch()
        self.grid.addWidget(card, row, col)
        self.cards[title] = value_label

    def refresh(self) -> None:
        stats = account_service.account_stats()
        media = media_service.media_stats()
        activation = activation_service.license_status()
        tasks = task_service.task_stats(20)

        self.license_label.setText(f"授权：{activation['statusText']}")
        mode = str(activation.get("mode") or "")
        if mode == "activated":
            role = "success"
        elif mode == "trial":
            role = "warning"
        else:
            role = "danger"
        self.license_label.setProperty("role", role)
        self.license_label.style().unpolish(self.license_label)
        self.license_label.style().polish(self.license_label)

        while self.grid.count():
            widget = self.grid.takeAt(0).widget()
            if widget:
                widget.deleteLater()
        self.cards.clear()

        stale = int(stats.get("stale") or 0)
        confirmed_abnormal = max(0, int(stats["abnormal"]) - stale)
        self._card(
            0,
            0,
            "账号健康",
            f"{stats['normal']} / {stats['total']}",
            f"{stats['profiles']} 个主体 · 异常 {confirmed_abnormal} · 待检测 {stale}",
        )
        self._card(
            0,
            1,
            "素材库",
            str(media["total"]),
            f"视频 {media['videos']} · 图片 {media['images']}",
            tone="blue",
        )
        self._card(
            0,
            2,
            "发布任务",
            str(tasks["total"]),
            f"今日 {tasks['today']} · 进行中 {tasks['active']}",
            tone="amber",
        )
        self._card(
            0,
            3,
            "任务结果",
            str(tasks["success"]),
            f"成功 · 失败 {tasks['failed']}",
            tone="coral" if int(tasks["failed"] or 0) else "primary",
        )

        platforms = stats.get("platforms") or []
        self.platform_table.clearSpans()
        self.platform_table.setRowCount(len(platforms))
        for row, item in enumerate(platforms):
            platform = str(item.get("platformName") or "")
            values = [platform, item.get("total"), item.get("normal"), item.get("abnormal")]
            for column, value in enumerate(values):
                color = PLATFORM_COLORS.get(platform) if column == 0 else None
                if column == 2 and int(item.get("normal") or 0):
                    color = COLORS["success"]
                if column == 3 and int(item.get("abnormal") or 0):
                    color = COLORS["danger"]
                self.platform_table.setItem(row, column, table_item(value, color))
            self.platform_table.setRowHeight(row, 40)
        if not platforms:
            self.platform_table.setRowCount(1)
            empty = table_item("暂无账号平台数据", COLORS["muted"])
            empty.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.platform_table.setItem(0, 0, empty)
            self.platform_table.setSpan(0, 0, 1, 4)
            self.platform_table.setRowHeight(0, 48)

        latest = tasks.get("latest") or []
        self.latest_count.setText(f"{len(latest)} 条")
        self.task_table.clearSpans()
        self.task_table.setRowCount(len(latest))
        for row, task in enumerate(latest):
            status = str(task.get("status") or "")
            values = [
                task.get("title") or task.get("taskNo") or "未命名任务",
                task.get("platformSummary") or "未记录",
                TASK_STATUS.get(status, status or "未知"),
                task.get("createdAt") or "",
            ]
            for column, value in enumerate(values):
                color = TASK_STATUS_COLORS.get(status) if column == 2 else None
                item = table_item(value, color)
                item.setToolTip(str(value or ""))
                self.task_table.setItem(row, column, item)
            self.task_table.setRowHeight(row, 40)

        if not latest:
            self.task_table.setRowCount(1)
            empty = table_item("暂无发布任务", COLORS["muted"])
            self.task_table.setItem(0, 0, empty)
            self.task_table.setSpan(0, 0, 1, 4)
