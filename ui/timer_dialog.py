# -*- coding: utf-8 -*-
"""定时发布设置弹窗。"""

from __future__ import annotations

from PyQt6.QtCore import QDate, QTime
from PyQt6.QtWidgets import (
    QCheckBox,
    QDateEdit,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QTimeEdit,
    QVBoxLayout,
)

from .common import button


class TimerDialog(QDialog):
    def __init__(self, parent=None, values: dict | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("定时发布")
        self.resize(440, 280)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 20)
        layout.setSpacing(12)
        heading = QLabel("定时发布")
        heading.setObjectName("dialogTitle")
        layout.addWidget(heading)
        panel = QFrame()
        panel.setProperty("subPanel", True)
        form = QFormLayout(panel)
        form.setContentsMargins(14, 12, 14, 12)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(9)
        self.enabled = QCheckBox("启用定时发布")
        self.publish_date = QDateEdit()
        self.publish_date.setCalendarPopup(True)
        self.publish_date.setDisplayFormat("yyyy-MM-dd")
        self.publish_date.setMinimumDate(QDate.currentDate())
        self.publish_date.setDate(QDate.currentDate().addDays(1))
        self.daily_time = QTimeEdit()
        self.daily_time.setDisplayFormat("HH:mm")
        self.daily_time.setTime(QTime(18, 0))

        current = values or {}
        schedule_time = str(current.get("scheduleTime") or "")
        if schedule_time:
            date_part, _, time_part = schedule_time.replace("T", " ").partition(" ")
            parsed_date = QDate.fromString(date_part, "yyyy-MM-dd")
            parsed_time = QTime.fromString(time_part[:5], "HH:mm")
            if parsed_date.isValid():
                self.publish_date.setDate(parsed_date)
            if parsed_time.isValid():
                self.daily_time.setTime(parsed_time)
        self.enabled.setChecked(bool(current.get("enableTimer")))

        form.addRow(self.enabled)
        form.addRow("发布日期", self.publish_date)
        form.addRow("发布时间", self.daily_time)
        layout.addWidget(panel)
        actions = QHBoxLayout()
        actions.addStretch()
        ok = button("保存", variant="primary")
        ok.clicked.connect(self.accept)
        actions.addWidget(ok)
        layout.addLayout(actions)

    def values(self) -> dict:
        date_text = self.publish_date.date().toString("yyyy-MM-dd")
        time_text = self.daily_time.time().toString("HH:mm")
        return {
            "enableTimer": self.enabled.isChecked(),
            "scheduleTime": f"{date_text} {time_text}",
            "videosPerDay": 1,
            "dailyTimes": [time_text],
            "startDays": 0,
            "timeJitterMinutes": 0,
        }
