# -*- coding: utf-8 -*-
"""抖音图文矩阵的逐账号字段编辑表。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, Mapping

from PyQt6.QtCore import QDateTime, QSignalBlocker, Qt
from PyQt6.QtWidgets import (
    QDateTimeEdit,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .common import button


FIELDS = ("title", "body", "tags")


class DouyinGraphicAccountRow(QFrame):
    """一个抖音账号的内容和排期。"""

    def __init__(self, account: Mapping[str, object], owner: "DouyinGraphicAccountTable") -> None:
        super().__init__()
        self.account = dict(account)
        self.owner = owner
        self.overridden_fields: set[str] = set()
        self.schedule_overridden = False
        self.setObjectName("douyinGraphicAccountRow")
        self.setProperty("subPanel", True)

        grid = QGridLayout(self)
        grid.setContentsMargins(12, 10, 12, 10)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)

        self.account_label = QLabel(self.display_name())
        self.account_label.setObjectName("douyinGraphicAccountName")
        self.account_label.setMinimumWidth(150)
        self.account_label.setToolTip(self.display_name())
        grid.addWidget(self.account_label, 0, 0, 2, 1)

        self.title_edit = self._field_edit("标题", "title")
        self.body_edit = self._field_edit("正文", "body")
        self.tags_edit = self._field_edit("话题", "tags")
        self.schedule_edit = QDateTimeEdit()
        self.schedule_edit.setObjectName("douyinGraphicAccountSchedule")
        self.schedule_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.schedule_edit.setCalendarPopup(True)
        self.schedule_edit.dateTimeChanged.connect(self._mark_schedule_overridden)

        grid.addWidget(QLabel("标题"), 0, 1)
        grid.addWidget(self.title_edit, 0, 2)
        grid.addWidget(self._restore_button("title"), 0, 3)
        grid.addWidget(QLabel("话题"), 0, 4)
        grid.addWidget(self.tags_edit, 0, 5)
        grid.addWidget(self._restore_button("tags"), 0, 6)
        grid.addWidget(QLabel("正文"), 1, 1)
        grid.addWidget(self.body_edit, 1, 2)
        grid.addWidget(self._restore_button("body"), 1, 3)
        grid.addWidget(QLabel("发布时间"), 1, 4)
        grid.addWidget(self.schedule_edit, 1, 5)
        schedule_reset = button("恢复排期", variant="secondary", compact=True)
        schedule_reset.clicked.connect(self.restore_schedule)
        grid.addWidget(schedule_reset, 1, 6)
        grid.setColumnStretch(2, 3)
        grid.setColumnStretch(5, 2)

    def _field_edit(self, placeholder: str, field: str) -> QLineEdit:
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.textEdited.connect(lambda _value, name=field: self.overridden_fields.add(name))
        return edit

    def _restore_button(self, field: str) -> QPushButton:
        restore = button("跟随通用", variant="secondary", compact=True)
        restore.setProperty("matrixField", field)
        restore.clicked.connect(lambda _checked=False, name=field: self.owner.restore_common_field(self.account_id, name))
        return restore

    @property
    def account_id(self) -> int:
        return int(self.account.get("id") or 0)

    def display_name(self) -> str:
        primary = str(self.account.get("profileName") or self.account.get("userName") or f"账号{self.account_id}")
        secondary = str(self.account.get("userName") or "").strip()
        return f"{primary} · {secondary}" if secondary and secondary != primary else primary

    def field_edit(self, field: str) -> QLineEdit:
        return {
            "title": self.title_edit,
            "body": self.body_edit,
            "tags": self.tags_edit,
        }[field]

    def set_field(self, field: str, value: str, *, overridden: bool | None = None) -> None:
        edit = self.field_edit(field)
        with QSignalBlocker(edit):
            edit.setText(value)
        if overridden is True:
            self.overridden_fields.add(field)
        elif overridden is False:
            self.overridden_fields.discard(field)

    def title(self) -> str:
        return self.title_edit.text().strip()

    def body(self) -> str:
        return self.body_edit.text().strip()

    def tags(self) -> list[str]:
        return [part.lstrip("#").strip() for part in self.tags_edit.text().replace("，", " ").split() if part.lstrip("#").strip()]

    def _mark_schedule_overridden(self) -> None:
        self.schedule_overridden = True

    def set_schedule(self, value: datetime, *, overridden: bool = False) -> None:
        with QSignalBlocker(self.schedule_edit):
            self.schedule_edit.setDateTime(QDateTime(value))
        self.schedule_overridden = overridden

    def restore_schedule(self) -> None:
        self.schedule_overridden = False
        self.owner._apply_generated_schedules()


class DouyinGraphicAccountTable(QWidget):
    """保存通用字段与逐账号覆盖的纯 UI 状态。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: dict[int, DouyinGraphicAccountRow] = {}
        self._common = {"title": "", "body": "", "tags": ""}
        self._schedule_start = datetime.now().replace(second=0, microsecond=0)
        self._interval_minutes = 30

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)
        header = QHBoxLayout()
        title = QLabel("逐账号设置")
        title.setObjectName("sectionTitle")
        self.count_label = QLabel("已选 0 个账号")
        self.count_label.setProperty("role", "caption")
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.count_label)
        root.addLayout(header)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.host = QWidget()
        self.rows_layout = QVBoxLayout(self.host)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(8)
        self.rows_layout.addStretch()
        self.scroll.setWidget(self.host)
        root.addWidget(self.scroll, 1)

    def set_accounts(self, accounts: Iterable[Mapping[str, object]]) -> None:
        rows = [dict(account) for account in accounts if int(account.get("type") or 0) == 3]
        if len(rows) > 20:
            raise ValueError("抖音图文矩阵每批最多选择 20 个账号")
        while self.rows_layout.count() > 1:
            item = self.rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._rows = {}
        for account in rows:
            row = DouyinGraphicAccountRow(account, self)
            self._rows[row.account_id] = row
            self.rows_layout.insertWidget(self.rows_layout.count() - 1, row)
            for field, value in self._common.items():
                row.set_field(field, value, overridden=False)
        self.count_label.setText(f"已选 {len(rows)} 个账号")
        self._apply_generated_schedules()

    def set_common_fields(self, title: str, body: str, tags: Iterable[str]) -> None:
        self._common = {
            "title": str(title or "").strip(),
            "body": str(body or "").strip(),
            "tags": " ".join(f"#{str(tag).lstrip('#').strip()}" for tag in tags if str(tag).lstrip("#").strip()),
        }
        for row in self._rows.values():
            for field, value in self._common.items():
                if field not in row.overridden_fields:
                    row.set_field(field, value, overridden=False)

    def set_schedule(self, start: datetime, interval_minutes: int) -> None:
        self._schedule_start = start.replace(second=0, microsecond=0)
        self._interval_minutes = max(1, int(interval_minutes))
        self._apply_generated_schedules()

    def _apply_generated_schedules(self) -> None:
        for index, row in enumerate(self._rows.values()):
            if not row.schedule_overridden:
                row.set_schedule(
                    self._schedule_start + timedelta(minutes=self._interval_minutes * index),
                    overridden=False,
                )

    def restore_common_field(self, account_id: int, field: str) -> None:
        if field not in FIELDS:
            raise ValueError("未知的账号字段")
        row = self.row_for(account_id)
        row.set_field(field, self._common[field], overridden=False)

    def edit_title(self, account_id: int, title: str) -> None:
        self.row_for(account_id).set_field("title", str(title), overridden=True)

    def row_for(self, account_id: int) -> DouyinGraphicAccountRow:
        try:
            return self._rows[int(account_id)]
        except KeyError as exc:
            raise ValueError("抖音图文账号不在当前批次中") from exc

    def targets(self) -> list[dict]:
        targets: list[dict] = []
        for item_index, row in enumerate(self._rows.values(), start=1):
            overrides: dict[str, object] = {}
            if "title" in row.overridden_fields:
                overrides["title"] = row.title()
            if "body" in row.overridden_fields:
                overrides["body"] = row.body()
            if "tags" in row.overridden_fields:
                overrides["tags"] = row.tags()
            targets.append(
                {
                    "itemIndex": item_index,
                    "accountId": row.account_id,
                    "overrides": overrides,
                    "scheduleTime": row.schedule_edit.dateTime().toString("yyyy-MM-dd HH:mm"),
                    "timezone": "Asia/Shanghai",
                    "scheduleOverridden": row.schedule_overridden,
                }
            )
        return targets
