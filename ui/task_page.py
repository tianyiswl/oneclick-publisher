# -*- coding: utf-8 -*-
"""任务记录页面。"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QCheckBox, QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QTableWidget, QTableWidgetItem, QTabWidget, QTextEdit, QVBoxLayout, QWidget

from app_core import task_service

from .common import button, table_item


STATUS_LABELS = {
    "pending": "等待执行",
    "running": "执行中",
    "success": "成功",
    "partial_failed": "部分失败",
    "failed": "失败",
    "cancelled": "已取消",
}

STATUS_COLORS = {
    "pending": "#64748b",
    "running": "#2563eb",
    "success": "#059669",
    "partial_failed": "#d97706",
    "failed": "#dc2626",
    "cancelled": "#64748b",
}


class TaskDetailDialog(QDialog):
    """任务记录明细弹窗。"""

    def __init__(self, task: dict, parent=None) -> None:
        super().__init__(parent)
        self.task = task
        self.setWindowTitle(f"任务明细 - {task.get('taskNo')}")
        self.resize(980, 680)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 20)
        layout.setSpacing(12)
        heading = QLabel("任务明细")
        heading.setObjectName("dialogTitle")
        layout.addWidget(heading)

        summary_panel = QFrame()
        summary_panel.setProperty("subPanel", True)
        summary = QGridLayout(summary_panel)
        summary.setContentsMargins(14, 12, 14, 12)
        summary.setHorizontalSpacing(14)
        summary.setVerticalSpacing(8)
        summary_values = [
            ("任务号", task.get("taskNoDisplay") or task.get("taskNo"), 0, 0, 1),
            ("任务类型", task.get("contentTypeLabel"), 0, 2, 1),
            ("任务场景", task.get("workflowLabel"), 0, 4, 1),
            (
                "状态",
                STATUS_LABELS.get(task.get("status"), task.get("status") or ""),
                1,
                0,
                1,
            ),
            ("标题", task.get("title"), 1, 2, 3),
            ("账号", task.get("accountSummary"), 2, 0, 5),
            ("平台", task.get("platformSummary"), 3, 0, 5),
            ("创建时间", task.get("createdAt"), 4, 0, 1),
            ("完成时间", task.get("finishedAt"), 4, 2, 1),
        ]
        if task.get("commerceSummary"):
            summary_values.append(("带货信息", task.get("commerceSummary"), 5, 0, 5))
        summary_values.append(("失败原因", task.get("lastError"), 6, 0, 5))
        for label_text, value, row, column, span in summary_values:
            label = QLabel(label_text)
            label.setProperty("role", "caption")
            value_label = QLabel(str(value or ""))
            value_label.setWordWrap(True)
            summary.addWidget(label, row, column)
            summary.addWidget(value_label, row, column + 1, 1, span)
        summary.setColumnStretch(1, 1)
        summary.setColumnStretch(3, 1)
        layout.addWidget(summary_panel)

        tabs = QTabWidget()
        tabs.addTab(self._items_tab(), "执行项")
        tabs.addTab(self._events_tab(), "事件日志")
        tabs.addTab(self._payload_tab(), "发布参数")
        layout.addWidget(tabs)

    def _items_tab(self) -> QTableWidget:
        rows = self.task.get("items") or []
        table = QTableWidget(len(rows), 10)
        table.setHorizontalHeaderLabels(["平台", "账号主体", "账号名", "备注", "素材", "状态", "说明", "尝试", "开始时间", "完成时间"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for row_idx, row in enumerate(rows):
            values = [
                row.get("platformName"),
                row.get("profileName") or row.get("accountLabel"),
                row.get("userName"),
                row.get("accountRemark"),
                row.get("fileName") or row.get("filePath"),
                STATUS_LABELS.get(row.get("status"), row.get("status")),
                row.get("message"),
                row.get("attempts"),
                row.get("startedAt"),
                row.get("finishedAt"),
            ]
            for col, value in enumerate(values):
                color = STATUS_COLORS.get(row.get("status")) if col == 5 else None
                table.setItem(row_idx, col, table_item(value, color))
        return table

    def _events_tab(self) -> QTableWidget:
        rows = self.task.get("events") or []
        table = QTableWidget(len(rows), 5)
        table.setHorizontalHeaderLabels(["时间", "级别", "事件", "说明", "详情"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for row_idx, row in enumerate(rows):
            values = [
                row.get("createdAt"),
                row.get("level"),
                row.get("eventType"),
                row.get("message"),
                row.get("detailJson"),
            ]
            for col, value in enumerate(values):
                color = "#dc2626" if row.get("level") == "error" else "#d97706" if row.get("level") == "warning" else None
                table.setItem(row_idx, col, table_item(value, color if col == 1 else None))
        return table

    def _payload_tab(self) -> QTextEdit:
        viewer = QTextEdit()
        viewer.setReadOnly(True)
        viewer.setPlainText(str(self.task.get("payloadJson") or ""))
        return viewer


class TaskPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("pageRoot")
        self.all_rows: list[dict] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(14)

        header_row = QHBoxLayout()
        title = QLabel("任务记录")
        title.setObjectName("pageTitle")
        header_row.addWidget(title)
        self.result_label = QLabel("0 条")
        self.result_label.setProperty("role", "countBadge")
        header_row.addWidget(self.result_label)
        header_row.addStretch()
        self.select_all_checkbox = QCheckBox("全选")
        self.select_all_checkbox.toggled.connect(self.set_all_checked)
        header_row.addWidget(self.select_all_checkbox)
        self.delete_btn = button("删除任务", variant="danger")
        self.delete_btn.setEnabled(False)
        self.delete_btn.clicked.connect(self.delete_selected_tasks)
        header_row.addWidget(self.delete_btn)
        detail = button("查看明细", variant="secondary")
        detail.clicked.connect(self.open_selected_detail)
        header_row.addWidget(detail)
        refresh = button("刷新", variant="primary")
        refresh.clicked.connect(self.refresh)
        header_row.addWidget(refresh)
        layout.addLayout(header_row)

        filters = QFrame()
        filters.setProperty("toolbar", True)
        filter_layout = QGridLayout(filters)
        filter_layout.setContentsMargins(14, 10, 14, 10)
        filter_layout.setHorizontalSpacing(10)
        keyword_label = QLabel("搜索")
        keyword_label.setProperty("role", "caption")
        status_label = QLabel("任务状态")
        status_label.setProperty("role", "caption")
        platform_label = QLabel("发布平台")
        platform_label.setProperty("role", "caption")
        content_type_label = QLabel("任务类型")
        content_type_label.setProperty("role", "caption")
        filter_layout.addWidget(keyword_label, 0, 0)
        filter_layout.addWidget(status_label, 0, 1)
        filter_layout.addWidget(platform_label, 0, 2)
        filter_layout.addWidget(content_type_label, 0, 3)

        self.keyword_input = QLineEdit()
        self.keyword_input.setPlaceholderText("搜索任务号、标题、账号主体、平台")
        self.keyword_input.textChanged.connect(self.apply_filters)
        self.status_filter = QComboBox()
        self.status_filter.currentIndexChanged.connect(self.apply_filters)
        self.platform_filter = QComboBox()
        self.platform_filter.currentIndexChanged.connect(self.apply_filters)
        self.content_type_filter = QComboBox()
        self.content_type_filter.currentIndexChanged.connect(self.apply_filters)
        filter_layout.addWidget(self.keyword_input, 1, 0)
        filter_layout.addWidget(self.status_filter, 1, 1)
        filter_layout.addWidget(self.platform_filter, 1, 2)
        filter_layout.addWidget(self.content_type_filter, 1, 3)
        filter_layout.setColumnStretch(0, 1)
        layout.addWidget(filters)

        self.table = QTableWidget(0, 10)
        self.table.setObjectName("dataTable")
        self.table.setHorizontalHeaderLabels(
            ["选择", "任务号", "任务类型", "任务场景", "标题", "状态", "账号", "平台", "执行结果", "创建时间"]
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 48)
        self.table.setColumnWidth(1, 128)
        self.table.setColumnWidth(2, 86)
        self.table.setColumnWidth(3, 98)
        self.table.setColumnWidth(5, 90)
        self.table.setColumnWidth(6, 210)
        self.table.setColumnWidth(7, 110)
        self.table.setColumnWidth(8, 150)
        self.table.setColumnWidth(9, 158)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemChanged.connect(self.update_selection_controls)
        self.table.cellDoubleClicked.connect(lambda row, _col: self.open_detail(row))
        layout.addWidget(self.table)
        self.refresh()

    def refresh(self) -> None:
        self.all_rows = task_service.list_tasks()
        self.refresh_filters()
        self.apply_filters()

    def refresh_filters(self) -> None:
        current_status = self.status_filter.currentData()
        current_platform = self.platform_filter.currentText()
        current_content_type = self.content_type_filter.currentData()

        self.status_filter.blockSignals(True)
        self.status_filter.clear()
        self.status_filter.addItem("全部状态", None)
        for status, label in STATUS_LABELS.items():
            self.status_filter.addItem(label, status)
        if current_status is not None:
            index = self.status_filter.findData(current_status)
            if index >= 0:
                self.status_filter.setCurrentIndex(index)
        self.status_filter.blockSignals(False)

        platforms = sorted(
            {
                platform.strip()
                for row in self.all_rows
                for platform in str(row.get("platformSummary") or "").split("、")
                if platform.strip()
            }
        )
        self.platform_filter.blockSignals(True)
        self.platform_filter.clear()
        self.platform_filter.addItem("全部平台")
        self.platform_filter.addItems(platforms)
        if current_platform in ["全部平台", *platforms]:
            self.platform_filter.setCurrentText(current_platform)
        self.platform_filter.blockSignals(False)

        content_types = sorted(
            {str(row.get("contentType") or "") for row in self.all_rows if row.get("contentType")}
        )
        self.content_type_filter.blockSignals(True)
        self.content_type_filter.clear()
        self.content_type_filter.addItem("全部类型", None)
        for content_type in content_types:
            self.content_type_filter.addItem(task_service.content_type_label(content_type), content_type)
        if current_content_type:
            index = self.content_type_filter.findData(current_content_type)
            if index >= 0:
                self.content_type_filter.setCurrentIndex(index)
        self.content_type_filter.blockSignals(False)

    def apply_filters(self) -> None:
        keyword = self.keyword_input.text().strip().lower()
        selected_status = self.status_filter.currentData()
        selected_platform = self.platform_filter.currentText()
        selected_content_type = self.content_type_filter.currentData()
        rows = []
        for row in self.all_rows:
            if selected_status and row.get("status") != selected_status:
                continue
            if selected_platform and selected_platform != "全部平台" and selected_platform not in str(row.get("platformSummary") or ""):
                continue
            if selected_content_type and row.get("contentType") != selected_content_type:
                continue
            searchable = " ".join(
                str(row.get(key) or "")
                for key in ("taskNo", "contentTypeLabel", "workflowLabel", "commerceSummary", "title", "accountSummary", "platformSummary", "createdAt", "finishedAt", "lastError")
            ).lower()
            if keyword and keyword not in searchable:
                continue
            rows.append(row)
        self.render_rows(rows)

    def render_rows(self, rows: list[dict]) -> None:
        self.result_label.setText(f"{len(rows)} 条")
        self.table.blockSignals(True)
        self.table.clearSpans()
        self.table.setRowCount(len(rows))
        for row_idx, row in enumerate(rows):
            select_item = QTableWidgetItem()
            select_flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            is_active = row.get("status") in {"pending", "running"}
            if not is_active:
                select_flags |= Qt.ItemFlag.ItemIsUserCheckable
                select_item.setCheckState(Qt.CheckState.Unchecked)
            else:
                select_item.setToolTip("等待执行或执行中的任务不能删除")
            select_item.setFlags(select_flags)
            select_item.setData(Qt.ItemDataRole.UserRole, row.get("id"))
            select_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row_idx, 0, select_item)
            account_parts = self._account_parts(row)
            account_text = account_parts["profiles"]
            if account_parts["users"]:
                account_text = (
                    f"{account_text}\n{account_parts['users']}"
                    if account_text
                    else account_parts["users"]
                )
            result_text = (
                f"{int(row.get('itemCount') or 0)} 项 · "
                f"成功 {int(row.get('successCount') or 0)} · "
                f"失败 {int(row.get('failedCount') or 0)}"
            )
            values = [
                row.get("taskNoDisplay") or row.get("taskNo"),
                row.get("contentTypeLabel"),
                row.get("workflowLabel"),
                row.get("title"),
                STATUS_LABELS.get(row.get("status"), row.get("status")),
                account_text,
                row.get("platformSummary"),
                result_text,
                row.get("createdAt"),
            ]
            for col, value in enumerate(values):
                color = None
                if col == 4:
                    color = STATUS_COLORS.get(row.get("status"))
                elif col == 7 and row.get("failedCount"):
                    color = "#dc2626"
                elif col == 7 and row.get("successCount"):
                    color = "#059669"
                elif col == 6:
                    color = "#2563eb"
                item = table_item(value, color)
                if col == 0 and row.get("taskNoDisplay") and row.get("taskNoDisplay") != row.get("taskNo"):
                    item.setToolTip(f"完整任务号：{row.get('taskNo')}")
                    self.table.setItem(row_idx, col + 1, item)
                    continue
                if col == 5 and account_parts["remarks"]:
                    item.setToolTip(
                        f"{value or ''}\n备注：{account_parts['remarks']}"
                    )
                else:
                    item.setToolTip(str(value or ""))
                self.table.setItem(row_idx, col + 1, item)
            self.table.setRowHeight(row_idx, 52)
        if not rows:
            self.table.setRowCount(1)
            empty = table_item("暂无任务记录")
            empty.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(0, 0, empty)
            self.table.setSpan(0, 0, 1, self.table.columnCount())
            self.table.setRowHeight(0, 52)
        self.table.blockSignals(False)
        self.update_selection_controls()

    def set_all_checked(self, checked: bool) -> None:
        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.table.blockSignals(False)
        self.update_selection_controls()

    def selected_task_ids(self) -> list[int]:
        selected = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                selected.append(int(item.data(Qt.ItemDataRole.UserRole)))
        return selected

    def update_selection_controls(self, *_args) -> None:
        checkable = []
        checked = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if not item or not item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                continue
            checkable.append(item)
            if item.checkState() == Qt.CheckState.Checked:
                checked.append(item)
        self.select_all_checkbox.blockSignals(True)
        self.select_all_checkbox.setChecked(bool(checkable) and len(checked) == len(checkable))
        self.select_all_checkbox.blockSignals(False)
        self.delete_btn.setText(f"删除任务（{len(checked)}）" if checked else "删除任务")
        self.delete_btn.setEnabled(bool(checked))

    def delete_selected_tasks(self) -> None:
        task_ids = self.selected_task_ids()
        if not task_ids:
            QMessageBox.information(self, "删除任务", "请先勾选需要删除的任务。")
            return
        answer = QMessageBox.question(
            self,
            "删除任务",
            f"确定删除选中的 {len(task_ids)} 个任务？任务明细和执行日志也会一并删除。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            deleted = task_service.delete_tasks(task_ids)
        except ValueError as exc:
            QMessageBox.warning(self, "删除任务", str(exc))
            return
        self.refresh()
        QMessageBox.information(self, "删除任务", f"已删除 {deleted} 个任务。")

    def _account_parts(self, row: dict) -> dict[str, str]:
        summary = str(row.get("accountSummary") or "")
        profiles: list[str] = []
        users: list[str] = []
        remarks: list[str] = []
        for account in summary.split("；"):
            pieces = [part.strip() for part in account.split("|")]
            if pieces and pieces[0] and pieces[0] not in profiles:
                profiles.append(pieces[0])
            if len(pieces) > 1 and pieces[1] and pieces[1] not in users:
                users.append(pieces[1])
            if len(pieces) > 2 and pieces[2] and pieces[2] not in remarks:
                remarks.append(pieces[2])
        return {
            "profiles": "；".join(profiles),
            "users": "；".join(users),
            "remarks": "；".join(remarks),
        }

    def selected_task_id(self) -> int | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return int(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def open_selected_detail(self) -> None:
        task_id = self.selected_task_id()
        if not task_id:
            QMessageBox.information(self, "任务明细", "请先选择一个任务。")
            return
        self.open_detail_by_id(task_id)

    def open_detail(self, row: int) -> None:
        item = self.table.item(row, 0)
        if not item:
            return
        task_id = int(item.data(Qt.ItemDataRole.UserRole))
        self.open_detail_by_id(task_id)

    def open_detail_by_id(self, task_id: int) -> None:
        task = task_service.get_task(task_id)
        if not task:
            QMessageBox.warning(self, "任务明细", "任务记录不存在或已被删除。")
            return
        TaskDetailDialog(task, self).exec()
