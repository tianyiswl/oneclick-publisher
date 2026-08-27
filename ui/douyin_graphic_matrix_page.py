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
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
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

from app_core import (
    account_service,
    douyin_graphic_matrix_draft_service,
    media_service,
    publish_service,
    task_service,
)
from app_core.douyin_graphic_matrix_service import SCHEMA_VERSION, WORKFLOW

from .common import button
from .douyin_graphic_matrix_table import DouyinGraphicAccountTable
from .topic_tag_editor import TopicTagEditor


SHANGHAI = ZoneInfo("Asia/Shanghai")


class DouyinGraphicMediaDialog(QDialog):
    """从素材管理中选择本批图文图片，筛选不会丢失既有勾选。"""

    def __init__(
        self,
        media_rows: Iterable[Mapping[str, object]],
        selected_paths: Iterable[str] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("从素材管理选择图片")
        self.resize(760, 580)
        self._rows = [
            dict(row)
            for row in media_rows
            if str(row.get("typeText") or "") == "图片"
            and Path(str(row.get("storedPath") or "")).is_file()
        ]
        self._selected_paths = list(
            dict.fromkeys(
                str(Path(path).expanduser().resolve())
                for path in selected_paths
                if str(path).strip()
            )
        )[:35]

        layout = QVBoxLayout(self)
        filters = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("搜索图片名称或备注")
        self.search_input.textChanged.connect(self._render_rows)
        self.category_combo = QComboBox()
        categories = list(
            dict.fromkeys(
                str(row.get("mediaCategory") or "其他").strip() or "其他"
                for row in self._rows
            )
        )
        self.category_combo.addItem("全部分类", "全部")
        for category in categories:
            self.category_combo.addItem(category, category)
        self.category_combo.currentIndexChanged.connect(self._render_rows)
        filters.addWidget(self.search_input, 1)
        filters.addWidget(self.category_combo)
        layout.addLayout(filters)

        self.media_list = QListWidget()
        self.media_list.setAlternatingRowColors(True)
        self.media_list.itemChanged.connect(self._item_changed)
        layout.addWidget(self.media_list, 1)

        selection_row = QHBoxLayout()
        self.selection_status = QLabel()
        self.selection_status.setProperty("role", "caption")
        selection_row.addWidget(self.selection_status)
        selection_row.addStretch()
        self.select_all_button = button("全选当前筛选", variant="secondary")
        self.select_all_button.clicked.connect(self._select_visible)
        self.deselect_all_button = button("取消全选", variant="secondary")
        self.deselect_all_button.clicked.connect(self._deselect_all)
        selection_row.addWidget(self.select_all_button)
        selection_row.addWidget(self.deselect_all_button)
        layout.addLayout(selection_row)

        actions = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Ok
        )
        actions.button(QDialogButtonBox.StandardButton.Ok).setText("使用所选图片")
        actions.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        actions.accepted.connect(self.accept)
        actions.rejected.connect(self.reject)
        layout.addWidget(actions)
        self._render_rows()

    def _filtered_rows(self) -> list[dict]:
        keyword = self.search_input.text().strip().lower()
        category = self.category_combo.currentData()
        return [
            row
            for row in self._rows
            if (category in (None, "全部") or row.get("mediaCategory") == category)
            and (
                not keyword
                or keyword
                in f"{row.get('filename') or ''} {row.get('remark') or ''}".lower()
            )
        ]

    def _render_rows(self) -> None:
        self.media_list.blockSignals(True)
        self.media_list.clear()
        selected = set(self._selected_paths)
        for row in self._filtered_rows():
            row = dict(row)
            path = str(Path(str(row.get("storedPath") or "")).resolve())
            remark = str(row.get("remark") or "").strip()
            label = str(row.get("filename") or Path(path).name)
            if remark:
                label += f" · {remark}"
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if path in selected
                else Qt.CheckState.Unchecked
            )
            row["storedPath"] = path
            item.setData(Qt.ItemDataRole.UserRole, row)
            item.setToolTip(path)
            self.media_list.addItem(item)
        self.media_list.blockSignals(False)
        self._refresh_status()

    def _item_changed(self, item: QListWidgetItem) -> None:
        row = item.data(Qt.ItemDataRole.UserRole) or {}
        path = str(row.get("storedPath") or "")
        if not path:
            return
        if item.checkState() == Qt.CheckState.Checked:
            if path not in self._selected_paths and len(self._selected_paths) >= 35:
                self.media_list.blockSignals(True)
                item.setCheckState(Qt.CheckState.Unchecked)
                self.media_list.blockSignals(False)
            elif path not in self._selected_paths:
                self._selected_paths.append(path)
        elif path in self._selected_paths:
            self._selected_paths.remove(path)
        self._refresh_status()

    def _select_visible(self) -> None:
        for row in self._filtered_rows():
            path = str(Path(str(row.get("storedPath") or "")).resolve())
            if path not in self._selected_paths:
                if len(self._selected_paths) >= 35:
                    break
                self._selected_paths.append(path)
        self._render_rows()

    def _deselect_all(self) -> None:
        self._selected_paths = []
        self._render_rows()

    def _refresh_status(self) -> None:
        suffix = "，已达到单批上限" if len(self._selected_paths) >= 35 else ""
        self.selection_status.setText(
            f"已选择 {len(self._selected_paths)} / 35 张{suffix}"
        )

    def selected_paths(self) -> list[str]:
        return list(self._selected_paths)


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
        self.save_draft_button = button("保存当前内容", variant="primary")
        self.save_draft_button.clicked.connect(self._save_matrix_draft_clicked)
        heading_row.addWidget(self.save_draft_button)
        self.restore_draft_button = button("恢复保存内容", variant="secondary")
        self.restore_draft_button.clicked.connect(self._restore_matrix_draft_clicked)
        heading_row.addWidget(self.restore_draft_button)
        self.clear_content_button = button("清空当前填写", variant="danger")
        self.clear_content_button.clicked.connect(self._clear_matrix_clicked)
        heading_row.addWidget(self.clear_content_button)
        root.addLayout(heading_row)

        self.draft_status = QLabel("图文矩阵内容尚未保存")
        self.draft_status.setProperty("role", "caption")
        root.addWidget(self.draft_status)

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
        self._refresh_draft_status()

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
        choose_material = button("从素材管理选择", variant="primary")
        choose_material.clicked.connect(self._choose_material_images)
        choose = button("从本机添加", variant="secondary")
        choose.clicked.connect(self._choose_images)
        remove = button("移除选中", variant="secondary")
        remove.clicked.connect(self._remove_selected_images)
        image_actions.addWidget(choose_material)
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
        account_header = QHBoxLayout()
        account_header.addWidget(QLabel("选择抖音账号（1–20 个）"))
        account_header.addStretch()
        self.account_selection_status = QLabel("已选择 0 个")
        self.account_selection_status.setProperty("role", "caption")
        account_header.addWidget(self.account_selection_status)
        self.select_all_accounts_button = button("全选", variant="ghost", compact=True)
        self.select_all_accounts_button.clicked.connect(self._select_all_accounts)
        account_header.addWidget(self.select_all_accounts_button)
        self.deselect_all_accounts_button = button("取消全选", variant="ghost", compact=True)
        self.deselect_all_accounts_button.clicked.connect(self._deselect_all_accounts)
        account_header.addWidget(self.deselect_all_accounts_button)
        content_layout.addLayout(account_header)
        self.account_list = QListWidget()
        self.account_list.setMaximumHeight(142)
        self.account_list.itemChanged.connect(self._account_item_changed)
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
        self._refresh_account_selection_status()

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
        self._refresh_account_selection_status()

    def _select_all_accounts(self) -> None:
        self.select_account_ids(
            int(self.account_list.item(index).data(Qt.ItemDataRole.UserRole))
            for index in range(min(self.account_list.count(), 20))
        )

    def _deselect_all_accounts(self) -> None:
        self.select_account_ids([])

    def _account_item_changed(self, item: QListWidgetItem) -> None:
        if (
            item.checkState() == Qt.CheckState.Checked
            and len(self.selected_account_ids()) > 20
        ):
            self.account_list.blockSignals(True)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.account_list.blockSignals(False)
        self._refresh_account_selection_status()

    def _refresh_account_selection_status(self) -> None:
        count = len(self.selected_account_ids())
        suffix = "，已达到单批上限" if count >= 20 else ""
        self.account_selection_status.setText(f"已选择 {count} 个{suffix}")

    def set_images(self, image_paths: Iterable[str]) -> None:
        values = [str(Path(path).expanduser().resolve()) for path in image_paths]
        if not 1 <= len(values) <= 35:
            raise ValueError("抖音图文图片数量必须为 1 至 35 张")
        self._set_image_paths(values)

    def _set_image_paths(self, image_paths: Iterable[str]) -> None:
        self._images = list(dict.fromkeys(str(path) for path in image_paths))[:35]
        self.image_list.clear()
        for index, path in enumerate(self._images, start=1):
            item = QListWidgetItem(f"{index}. {Path(path).name}")
            item.setToolTip(path)
            self.image_list.addItem(item)

    def image_paths(self) -> list[str]:
        return list(self._images)

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

    def _choose_material_images(self) -> None:
        dialog = DouyinGraphicMediaDialog(
            media_service.list_media(), self._images, self
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._set_image_paths(dialog.selected_paths())

    def _remove_selected_images(self) -> None:
        indexes = {index.row() for index in self.image_list.selectedIndexes()}
        remaining = [path for index, path in enumerate(self._images) if index not in indexes]
        self._set_image_paths(remaining)

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

    def _draft_payload(self) -> dict:
        media_ids: dict[str, int] = {}
        for row in media_service.list_media():
            path = str(row.get("storedPath") or "").strip()
            if path:
                media_ids[str(Path(path).expanduser().resolve())] = int(
                    row.get("id") or 0
                )
        selected_ids = set(self.selected_account_ids())
        targets = [
            target
            for target in self.account_table.targets()
            if int(target.get("accountId") or 0) in selected_ids
        ]
        return {
            "schemaVersion": douyin_graphic_matrix_draft_service.DRAFT_SCHEMA,
            "images": [
                {"mediaId": media_ids.get(path, 0), "path": path}
                for path in self._images
            ],
            "accounts": [
                {
                    "accountId": int(account.get("id") or 0),
                    "filePath": str(account.get("filePath") or ""),
                }
                for account in self._available_accounts
                if int(account.get("id") or 0) in selected_ids
            ],
            "common": {
                "title": self.common_title.text(),
                "body": self.common_body.toPlainText(),
                "tags": self.common_tags.tags(),
            },
            "schedule": {
                "startDate": self.start_date.date().toString("yyyy-MM-dd"),
                "startTime": self.start_time.time().toString("HH:mm"),
                "intervalMinutes": self.interval.value(),
            },
            "targets": targets,
            "currentStep": self._step,
        }

    def save_matrix_draft(self) -> dict:
        saved = douyin_graphic_matrix_draft_service.save_draft(
            self._draft_payload()
        )
        self.draft_status.setText(f"已保存 {saved.get('updatedAt') or ''}".strip())
        self.draft_status.setProperty("role", "success")
        self.restore_draft_button.setEnabled(True)
        return saved

    def _save_matrix_draft_clicked(self) -> None:
        try:
            self.save_matrix_draft()
        except douyin_graphic_matrix_draft_service.DouyinGraphicMatrixDraftError as exc:
            QMessageBox.warning(self, "保存图文矩阵", str(exc))
            return
        QMessageBox.information(
            self,
            "保存图文矩阵",
            "图片、账号、通用内容、逐账号设置和排期已保存在本机。\n"
            "该操作不会打开抖音，也不会提交内容。",
        )

    def _refresh_draft_status(self) -> None:
        try:
            draft = douyin_graphic_matrix_draft_service.load_draft()
        except douyin_graphic_matrix_draft_service.DouyinGraphicMatrixDraftError as exc:
            self.draft_status.setText(str(exc))
            self.draft_status.setProperty("role", "danger")
            self.restore_draft_button.setEnabled(False)
            return
        self.restore_draft_button.setEnabled(bool(draft))
        if draft:
            self.draft_status.setText(
                f"已保存 {draft.get('updatedAt') or ''}".strip()
            )
            self.draft_status.setProperty("role", "success")
        else:
            self.draft_status.setText("图文矩阵内容尚未保存")
            self.draft_status.setProperty("role", "caption")

    def restore_matrix_draft(self) -> dict[str, list[str]]:
        draft = douyin_graphic_matrix_draft_service.load_draft()
        if not draft:
            raise douyin_graphic_matrix_draft_service.DouyinGraphicMatrixDraftError(
                "当前没有已保存的图文矩阵内容"
            )
        payload = dict(draft.get("payload") or {})

        material_by_id = {
            int(row.get("id") or 0): dict(row)
            for row in media_service.list_media()
            if int(row.get("id") or 0) > 0
        }
        image_paths: list[str] = []
        missing_images: list[str] = []
        for reference in payload.get("images") or []:
            saved_path = str(reference.get("path") or "")
            material = material_by_id.get(int(reference.get("mediaId") or 0), {})
            candidate = str(material.get("storedPath") or saved_path)
            path = Path(candidate).expanduser()
            if path.is_file():
                resolved = str(path.resolve())
                if resolved not in image_paths:
                    image_paths.append(resolved)
            else:
                missing_images.append(Path(saved_path or candidate).name or "未知图片")
        self._set_image_paths(image_paths)

        accounts_by_id = {
            int(account.get("id") or 0): account
            for account in self._available_accounts
        }
        accounts_by_file = {
            str(account.get("filePath") or ""): account
            for account in self._available_accounts
            if str(account.get("filePath") or "")
        }
        restored_account_ids: list[int] = []
        restored_account_id_map: dict[int, int] = {}
        missing_accounts: list[str] = []
        for reference in payload.get("accounts") or []:
            saved_id = int(reference.get("accountId") or 0)
            account = accounts_by_id.get(saved_id) or accounts_by_file.get(
                str(reference.get("filePath") or "")
            )
            if account:
                account_id = int(account.get("id") or 0)
                restored_account_id_map[saved_id] = account_id
                if account_id not in restored_account_ids:
                    restored_account_ids.append(account_id)
            else:
                missing_accounts.append(str(saved_id))
        self.select_account_ids(restored_account_ids)

        common = dict(payload.get("common") or {})
        self.set_common_content(
            str(common.get("title") or ""),
            str(common.get("body") or ""),
            list(common.get("tags") or []),
        )
        schedule = dict(payload.get("schedule") or {})
        restored_date = QDate.fromString(
            str(schedule.get("startDate") or ""), "yyyy-MM-dd"
        )
        restored_time = QTime.fromString(
            str(schedule.get("startTime") or ""), "HH:mm"
        )
        if restored_date.isValid():
            self.start_date.setDate(restored_date)
        if restored_time.isValid():
            self.start_time.setTime(restored_time)
        self.interval.setValue(int(schedule.get("intervalMinutes") or 30))

        selected_accounts = [
            account
            for account in self._available_accounts
            if int(account.get("id") or 0) in restored_account_ids
        ]
        self.account_table.set_accounts(selected_accounts)
        self._common_changed()
        self._apply_schedule()
        target_by_account: dict[int, dict] = {}
        for target in payload.get("targets") or []:
            saved_id = int(target.get("accountId") or 0)
            current_id = restored_account_id_map.get(saved_id, saved_id)
            target_by_account[current_id] = dict(target)
        for account_id in restored_account_ids:
            target = target_by_account.get(account_id)
            if not target:
                continue
            row = self.account_table.row_for(account_id)
            for field, value in dict(target.get("overrides") or {}).items():
                if field not in {"title", "body", "tags"}:
                    continue
                text = (
                    " ".join(f"#{str(tag).lstrip('#')}" for tag in value)
                    if field == "tags" and isinstance(value, list)
                    else str(value)
                )
                row.set_field(field, text, overridden=True)
            schedule_text = str(target.get("scheduleTime") or "").strip()
            if schedule_text:
                row.set_schedule(
                    datetime.strptime(schedule_text, "%Y-%m-%d %H:%M"),
                    overridden=bool(target.get("scheduleOverridden")),
                )

        requested_step = int(payload.get("currentStep") or 0)
        if requested_step > 0 and (not image_paths or not restored_account_ids):
            requested_step = 0
        if requested_step == 2:
            self._refresh_summary()
        self._set_step(requested_step)
        self.draft_status.setText(
            f"已恢复 {draft.get('updatedAt') or ''}".strip()
        )
        return {
            "missingImages": missing_images,
            "missingAccounts": missing_accounts,
        }

    def _restore_matrix_draft_clicked(self) -> None:
        try:
            result = self.restore_matrix_draft()
        except douyin_graphic_matrix_draft_service.DouyinGraphicMatrixDraftError as exc:
            QMessageBox.warning(self, "恢复图文矩阵", str(exc))
            return
        missing = [
            *(f"图片：{name}" for name in result["missingImages"]),
            *(f"账号ID：{account_id}" for account_id in result["missingAccounts"]),
        ]
        detail = (
            "\n\n以下项目已不存在，未恢复：\n" + "\n".join(missing)
            if missing
            else ""
        )
        QMessageBox.information(
            self,
            "恢复图文矩阵",
            "已恢复上次保存的图文矩阵内容。" + detail,
        )

    def clear_matrix_state(self) -> None:
        self._set_image_paths([])
        self.set_common_content("", "", [])
        self.select_account_ids([])
        self.account_table.set_accounts([])
        tomorrow = datetime.now(SHANGHAI).date() + timedelta(days=1)
        self.start_date.setDate(QDate(tomorrow.year, tomorrow.month, tomorrow.day))
        self.start_time.setTime(QTime(18, 0))
        self.interval.setValue(30)
        self.summary_table.setRowCount(0)
        self._current_task_id = 0
        self.open_task_button.setEnabled(False)
        self.status_label.setText(
            "尚未检查。本地批量检查只核对图片、字段、账号和排期，不会打开抖音。"
        )
        self._set_step(0)
        self.draft_status.setText("当前填写已清空，可用“恢复保存内容”找回")

    def _clear_matrix_clicked(self) -> None:
        answer = QMessageBox.question(
            self,
            "清空图文矩阵",
            "确定清空当前图片、账号、文案、逐账号设置和排期吗？\n\n"
            "已经保存的内容不会删除，仍可恢复。",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.clear_matrix_state()

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

    def open_failed_retry(self, task_id: int) -> None:
        """只读加载失败/未开始账号，返回界面重新确认。"""

        try:
            prepared = task_service.prepare_douyin_graphic_matrix_retry(int(task_id))
            matrix = dict(prepared["matrix"])
            content = dict(matrix.get("content") or {})
            common = dict(content.get("common") or {})
            targets = [dict(target) for target in matrix.get("targets") or []]
            account_ids = [int(target.get("accountId") or 0) for target in targets]
            self.set_images(content.get("images") or [])
            self.set_common_content(
                str(common.get("title") or ""),
                str(common.get("body") or ""),
                list(common.get("tags") or []),
            )
            self.select_account_ids(account_ids)
            selected = [
                account
                for account in self._available_accounts
                if int(account.get("id") or 0) in account_ids
            ]
            self.account_table.set_accounts(selected)
            self._common_changed()
            target_by_account = {
                int(target.get("accountId") or 0): target for target in targets
            }
            for account_id in account_ids:
                row = self.account_table.row_for(account_id)
                target = target_by_account[account_id]
                overrides = dict(target.get("overrides") or {})
                for field in ("title", "body", "tags"):
                    value = overrides.get(field)
                    if value is None:
                        continue
                    text = (
                        " ".join(f"#{str(tag).lstrip('#')}" for tag in value)
                        if field == "tags" and isinstance(value, list)
                        else str(value)
                    )
                    row.set_field(field, text, overridden=True)
                schedule_text = str(target.get("scheduleTime") or "").strip()
                if schedule_text:
                    row.set_schedule(
                        datetime.strptime(schedule_text, "%Y-%m-%d %H:%M"),
                        overridden=bool(target.get("scheduleOverridden")),
                    )
            self._refresh_summary()
            self.status_label.setText(
                f"已从任务 {prepared.get('sourceTaskNo') or task_id} 加载 "
                f"{len(targets)} 个失败或未开始账号；请重新核对后再做本地检查。"
            )
            self._set_step(2)
        except Exception as exc:
            QMessageBox.warning(self, "重试图文矩阵", str(exc))

    def _set_step(self, index: int) -> None:
        self._step = max(0, min(int(index), 2))
        self.stack.setCurrentIndex(self._step)
        for label_index, label in enumerate(self.step_labels):
            label.setProperty("active", label_index == self._step)
            label.style().unpolish(label)
            label.style().polish(label)
