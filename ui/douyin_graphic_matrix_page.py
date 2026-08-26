# -*- coding: utf-8 -*-
"""独立的抖音图文矩阵三步式工作页。"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

from PyQt6.QtCore import QDate, QTime, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDateEdit,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from app_core import account_service, publish_service
from app_core.douyin_graphic_matrix_service import SCHEMA_VERSION, WORKFLOW

from .common import button
from .douyin_graphic_matrix_table import DouyinGraphicAccountTable
from .topic_tag_editor import TopicTagEditor


SHANGHAI = ZoneInfo("Asia/Shanghai")


class DouyinGraphicMatrixPage(QWidget):
    request_account_management = pyqtSignal()
    task_created = pyqtSignal(int)
    open_task_detail = pyqtSignal(int)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        accounts: Iterable[Mapping[str, object]] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("pageRoot")
        self._available_accounts: list[dict] = []
        self._images: list[str] = []
        self._current_task_id = 0
        self._step = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 18)
        root.setSpacing(14)

        heading_row = QHBoxLayout()
        heading_box = QVBoxLayout()
        heading = QLabel("抖音图文矩阵")
        heading.setObjectName("pageTitle")
        subtitle = QLabel("一套图文按账号分别设置标题、正文、话题和发布时间；每次只处理一个账号。")
        subtitle.setWordWrap(True)
        subtitle.setProperty("role", "caption")
        heading_box.addWidget(heading)
        heading_box.addWidget(subtitle)
        heading_row.addLayout(heading_box, 1)
        manage = button("管理抖音账号", variant="secondary")
        manage.clicked.connect(self.request_account_management.emit)
        heading_row.addWidget(manage)
        root.addLayout(heading_row)

        self.step_labels: list[QLabel] = []
        step_row = QHBoxLayout()
        step_row.setSpacing(8)
        for index, text in enumerate(("1  内容准备", "2  账号设置", "3  检查与提交")):
            label = QLabel(text)
            label.setObjectName("douyinGraphicStep")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setMinimumHeight(42)
            label.setProperty("active", index == 0)
            self.step_labels.append(label)
            step_row.addWidget(label, 1)
        root.addLayout(step_row)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_content_step())
        self.stack.addWidget(self._build_account_step())
        self.stack.addWidget(self._build_check_step())
        root.addWidget(self.stack, 1)

        if accounts is None:
            accounts = account_service.list_accounts()
        self.set_available_accounts(accounts)
        self._set_step(0)

    def _panel(self) -> tuple[QFrame, QVBoxLayout]:
        panel = QFrame()
        panel.setObjectName("douyinGraphicPanel")
        panel.setProperty("subPanel", True)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        return panel, layout

    def _build_content_step(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        media, media_layout = self._panel()
        media_title = QLabel("图片素材（1–35 张）")
        media_title.setObjectName("sectionTitle")
        media_layout.addWidget(media_title)
        self.image_list = QListWidget()
        self.image_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.image_list.setAlternatingRowColors(True)
        media_layout.addWidget(self.image_list, 1)
        image_actions = QHBoxLayout()
        choose = button("选择图片", variant="primary")
        choose.clicked.connect(self._choose_images)
        remove = button("移除选中", variant="secondary")
        remove.clicked.connect(self._remove_selected_images)
        image_actions.addWidget(choose)
        image_actions.addWidget(remove)
        image_actions.addStretch()
        media_layout.addLayout(image_actions)
        layout.addWidget(media, 3)

        content, content_layout = self._panel()
        content_title = QLabel("通用内容")
        content_title.setObjectName("sectionTitle")
        content_layout.addWidget(content_title)
        content_layout.addWidget(QLabel("标题"))
        self.common_title = QLineEdit()
        self.common_title.setPlaceholderText("输入所有账号默认使用的标题")
        self.common_title.textChanged.connect(self._common_changed)
        content_layout.addWidget(self.common_title)
        content_layout.addWidget(QLabel("正文"))
        self.common_body = QPlainTextEdit()
        self.common_body.setPlaceholderText("输入所有账号默认使用的正文")
        self.common_body.setMaximumHeight(130)
        self.common_body.textChanged.connect(self._common_changed)
        content_layout.addWidget(self.common_body)
        self.common_tags = TopicTagEditor(placeholder="例如：AI工具、效率提升", history_rows=1)
        self.common_tags.input.textChanged.connect(lambda _text: None)
        content_layout.addWidget(self.common_tags)
        content_layout.addWidget(QLabel("选择抖音账号（1–20 个）"))
        self.account_list = QListWidget()
        self.account_list.setMaximumHeight(142)
        content_layout.addWidget(self.account_list)
        next_button = button("下一步：账号设置", variant="primary")
        next_button.setObjectName("douyinGraphicNextButton")
        next_button.clicked.connect(self._go_account_step)
        content_layout.addWidget(next_button)
        layout.addWidget(content, 5)
        return page

    def _build_account_step(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        schedule_panel, schedule_layout = self._panel()
        schedule_row = QHBoxLayout()
        title = QLabel("统一排期")
        title.setObjectName("sectionTitle")
        schedule_row.addWidget(title)
        schedule_row.addStretch()
        schedule_row.addWidget(QLabel("起始日期（北京时间）"))
        self.start_date = QDateEdit()
        self.start_date.setCalendarPopup(True)
        tomorrow = datetime.now(SHANGHAI).date() + timedelta(days=1)
        self.start_date.setDate(QDate(tomorrow.year, tomorrow.month, tomorrow.day))
        schedule_row.addWidget(self.start_date)
        schedule_row.addWidget(QLabel("起始时间"))
        self.start_time = QTimeEdit(QTime(18, 0))
        self.start_time.setDisplayFormat("HH:mm")
        schedule_row.addWidget(self.start_time)
        schedule_row.addWidget(QLabel("账号间隔"))
        self.interval = QSpinBox()
        self.interval.setRange(1, 1440)
        self.interval.setValue(30)
        self.interval.setSuffix(" 分钟")
        schedule_row.addWidget(self.interval)
        apply_schedule = button("重新排期", variant="secondary")
        apply_schedule.clicked.connect(self._apply_schedule)
        schedule_row.addWidget(apply_schedule)
        schedule_layout.addLayout(schedule_row)
        layout.addWidget(schedule_panel)

        table_panel, table_layout = self._panel()
        self.account_table = DouyinGraphicAccountTable()
        table_layout.addWidget(self.account_table)
        layout.addWidget(table_panel, 1)

        actions = QHBoxLayout()
        previous = button("返回内容", variant="secondary")
        previous.clicked.connect(lambda: self._set_step(0))
        next_button = button("下一步：检查与提交", variant="primary")
        next_button.clicked.connect(self._go_check_step)
        actions.addWidget(previous)
        actions.addStretch()
        actions.addWidget(next_button)
        layout.addLayout(actions)
        return page

    def _build_check_step(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        panel, panel_layout = self._panel()
        title = QLabel("最终账号快照")
        title.setObjectName("sectionTitle")
        panel_layout.addWidget(title)
        self.summary_table = QTableWidget(0, 5)
        self.summary_table.setHorizontalHeaderLabels(("序号", "账号", "标题", "话题", "北京时间"))
        self.summary_table.verticalHeader().setVisible(False)
        self.summary_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.summary_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.summary_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.summary_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.summary_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.summary_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        panel_layout.addWidget(self.summary_table, 1)
        layout.addWidget(panel, 1)

        self.status_label = QLabel("尚未检查。本地批量检查只核对图片、字段、账号和排期，不会打开抖音。")
        self.status_label.setObjectName("infoCallout")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        actions = QHBoxLayout()
        previous = button("返回账号设置", variant="secondary")
        previous.clicked.connect(lambda: self._set_step(1))
        self.local_check_button = button("本地批量检查", variant="primary")
        self.local_check_button.setObjectName("douyinGraphicLocalCheckButton")
        self.local_check_button.clicked.connect(self._start_local_check)
        self.open_task_button = button("查看任务明细", variant="secondary")
        self.open_task_button.setEnabled(False)
        self.open_task_button.clicked.connect(self._emit_open_task)
        actions.addWidget(previous)
        actions.addStretch()
        actions.addWidget(self.open_task_button)
        actions.addWidget(self.local_check_button)
        layout.addLayout(actions)
        return page

    def refresh(self) -> None:
        self.set_available_accounts(account_service.list_accounts())

    def set_available_accounts(self, accounts: Iterable[Mapping[str, object]]) -> None:
        selected = set(self.selected_account_ids()) if hasattr(self, "account_list") else set()
        self._available_accounts = [dict(account) for account in accounts if int(account.get("type") or 0) == 3]
        self.account_list.clear()
        for account in self._available_accounts:
            account_id = int(account.get("id") or 0)
            label = str(account.get("profileName") or account.get("userName") or f"账号{account_id}")
            user = str(account.get("userName") or "").strip()
            item = QListWidgetItem(f"{label} · {user}" if user and user != label else label)
            item.setData(Qt.ItemDataRole.UserRole, account_id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if account_id in selected else Qt.CheckState.Unchecked)
            self.account_list.addItem(item)

    def available_accounts(self) -> list[dict]:
        return list(self._available_accounts)

    def selected_account_ids(self) -> list[int]:
        result: list[int] = []
        for index in range(self.account_list.count()):
            item = self.account_list.item(index)
            if item.checkState() == Qt.CheckState.Checked:
                result.append(int(item.data(Qt.ItemDataRole.UserRole)))
        return result

    def select_account_ids(self, account_ids: Iterable[int]) -> None:
        selected = {int(value) for value in account_ids}
        if len(selected) > 20:
            raise ValueError("抖音图文矩阵每批最多选择 20 个账号")
        known = {int(account.get("id") or 0) for account in self._available_accounts}
        if not selected.issubset(known):
            raise ValueError("选中了不存在的抖音账号")
        for index in range(self.account_list.count()):
            item = self.account_list.item(index)
            item.setCheckState(Qt.CheckState.Checked if int(item.data(Qt.ItemDataRole.UserRole)) in selected else Qt.CheckState.Unchecked)

    def set_images(self, image_paths: Iterable[str]) -> None:
        values = [str(Path(path).expanduser().resolve()) for path in image_paths]
        if not 1 <= len(values) <= 35:
            raise ValueError("抖音图文图片数量必须为 1 至 35 张")
        self._images = values
        self.image_list.clear()
        for index, path in enumerate(values, start=1):
            item = QListWidgetItem(f"{index}. {Path(path).name}")
            item.setToolTip(path)
            self.image_list.addItem(item)

    def set_common_content(self, title: str, body: str, tags: Iterable[str]) -> None:
        self.common_title.setText(str(title or ""))
        self.common_body.setPlainText(str(body or ""))
        self.common_tags.setPlainText(list(tags))
        self._common_changed()

    def _choose_images(self) -> None:
        paths, _filter = QFileDialog.getOpenFileNames(self, "选择图片", "", "图片 (*.jpg *.jpeg *.png *.webp)")
        if not paths:
            return
        try:
            self.set_images(paths)
        except ValueError as exc:
            QMessageBox.warning(self, "图片素材", str(exc))

    def _remove_selected_images(self) -> None:
        indexes = {index.row() for index in self.image_list.selectedIndexes()}
        remaining = [path for index, path in enumerate(self._images) if index not in indexes]
        self._images = remaining
        self.image_list.clear()
        for index, path in enumerate(remaining, start=1):
            self.image_list.addItem(f"{index}. {Path(path).name}")

    def _common_changed(self) -> None:
        if hasattr(self, "account_table"):
            self.account_table.set_common_fields(self.common_title.text(), self.common_body.toPlainText(), self.common_tags.tags())

    def _go_account_step(self) -> None:
        account_ids = self.selected_account_ids()
        if not self._images:
            QMessageBox.warning(self, "内容准备", "请先选择 1 至 35 张图片。")
            return
        if not self.common_title.text().strip() or not self.common_body.toPlainText().strip():
            QMessageBox.warning(self, "内容准备", "请填写通用标题和正文。")
            return
        if not 1 <= len(account_ids) <= 20:
            QMessageBox.warning(self, "内容准备", "请选择 1 至 20 个抖音账号。")
            return
        selected = [account for account in self._available_accounts if int(account.get("id") or 0) in account_ids]
        self.account_table.set_accounts(selected)
        self._common_changed()
        self._apply_schedule()
        self._set_step(1)

    def _apply_schedule(self) -> None:
        date_value = self.start_date.date()
        time_value = self.start_time.time()
        start = datetime.combine(
            date_value.toPyDate(),
            time(time_value.hour(), time_value.minute()),
            tzinfo=SHANGHAI,
        )
        self.account_table.set_schedule(start, self.interval.value())

    def _go_check_step(self) -> None:
        self._refresh_summary()
        self._set_step(2)

    def _refresh_summary(self) -> None:
        targets = self.account_table.targets()
        self.summary_table.setRowCount(len(targets))
        for row_index, target in enumerate(targets):
            row = self.account_table.row_for(target["accountId"])
            values = (
                target["itemIndex"],
                row.display_name(),
                row.title(),
                " ".join(f"#{tag}" for tag in row.tags()),
                target["scheduleTime"],
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                self.summary_table.setItem(row_index, column, item)

    def _matrix_payload(self, runtime_mode: str = "local_check") -> dict:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "workflow": WORKFLOW,
            "runtimeMode": runtime_mode,
            "content": {
                "images": list(self._images),
                "common": {
                    "title": self.common_title.text().strip(),
                    "body": self.common_body.toPlainText().strip(),
                    "tags": self.common_tags.tags(),
                },
            },
            "schedule": {
                "startDate": self.start_date.date().toString("yyyy-MM-dd"),
                "startTime": self.start_time.time().toString("HH:mm"),
                "intervalMinutes": self.interval.value(),
            },
            "targets": self.account_table.targets(),
        }

    def _start_local_check(self) -> None:
        try:
            task = publish_service.start_douyin_graphic_matrix(self._matrix_payload("local_check"))
        except Exception as exc:
            QMessageBox.warning(self, "本地批量检查", str(exc))
            return
        self._current_task_id = int(task.get("id") or 0)
        self.open_task_button.setEnabled(self._current_task_id > 0)
        self.status_label.setText(
            f"本地检查任务 {task.get('taskNoDisplay') or task.get('taskNo') or self._current_task_id} 已创建，尚未打开抖音。"
        )
        self.task_created.emit(self._current_task_id)

    def _emit_open_task(self) -> None:
        if self._current_task_id > 0:
            self.open_task_detail.emit(self._current_task_id)

    def _set_step(self, index: int) -> None:
        self._step = max(0, min(int(index), 2))
        self.stack.setCurrentIndex(self._step)
        for label_index, label in enumerate(self.step_labels):
            label.setProperty("active", label_index == self._step)
            label.style().unpolish(label)
            label.style().polish(label)
