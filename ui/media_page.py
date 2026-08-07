# -*- coding: utf-8 -*-
"""素材管理页面。"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox, QSplitter, QTableWidget, QVBoxLayout, QWidget

from app_core import media_service

from .common import ROOT_DIR, button, table_item
from .media_context_menu import build_media_context_menu
from .platform_open import open_path, reveal_in_folder


class MediaPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("pageRoot")
        self.setAcceptDrops(True)
        self.current_category = "全部"
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(14)

        header = QHBoxLayout()
        title = QLabel("素材管理")
        title.setObjectName("pageTitle")
        header.addWidget(title)
        self.selected_label = QLabel("已勾选 0 / 0")
        self.selected_label.setProperty("role", "countBadge")
        header.addWidget(self.selected_label)
        header.addStretch()
        cover_btn = button("刷新封面", variant="secondary")
        cover_btn.clicked.connect(self.refresh_selected_covers)
        header.addWidget(cover_btn)
        del_btn = button("删除选中", variant="danger")
        del_btn.clicked.connect(self.delete_selected)
        header.addWidget(del_btn)
        refresh_btn = button("刷新", variant="secondary")
        refresh_btn.clicked.connect(self.refresh)
        header.addWidget(refresh_btn)
        self.add_btn = button("导入素材", variant="primary")
        self.add_btn.clicked.connect(self.import_dialog)
        header.addWidget(self.add_btn)
        layout.addLayout(header)

        filters = QFrame()
        filters.setProperty("toolbar", True)
        filter_layout = QGridLayout(filters)
        filter_layout.setContentsMargins(14, 10, 14, 10)
        filter_layout.setHorizontalSpacing(10)
        search_label = QLabel("搜索")
        search_label.setProperty("role", "caption")
        filter_layout.addWidget(search_label, 0, 0)
        type_label = QLabel("素材类型")
        type_label.setProperty("role", "caption")
        filter_layout.addWidget(type_label, 0, 1)
        select_label = QLabel("批量选择")
        select_label.setProperty("role", "caption")
        filter_layout.addWidget(select_label, 0, 2)

        self.select_all = QCheckBox("全选")
        self.select_all.stateChanged.connect(self.toggle_all)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("搜索文件名或备注")
        self.search_input.textChanged.connect(self.refresh)
        self.type_filter = QComboBox()
        self.type_filter.addItems(["全部素材", "视频", "图片", "文件"])
        self.type_filter.currentIndexChanged.connect(self.refresh)
        filter_layout.addWidget(self.search_input, 1, 0)
        filter_layout.addWidget(self.type_filter, 1, 1)
        filter_layout.addWidget(self.select_all, 1, 2)
        filter_layout.setColumnStretch(0, 1)
        workspace = QSplitter(Qt.Orientation.Horizontal)
        workspace.setChildrenCollapsible(False)
        category_panel = QFrame()
        category_panel.setProperty("subPanel", True)
        category_layout = QVBoxLayout(category_panel)
        category_layout.setContentsMargins(12, 12, 12, 12)
        category_layout.setSpacing(8)
        category_title = QLabel("素材分类")
        category_title.setProperty("role", "sectionTitle")
        category_layout.addWidget(category_title)
        category_hint = QLabel("账号主体自动同步\n“其他”始终保留")
        category_hint.setProperty("role", "caption")
        category_layout.addWidget(category_hint)
        self.category_list = QListWidget()
        self.category_list.setObjectName("mediaCategoryList")
        self.category_list.currentItemChanged.connect(self.category_changed)
        category_layout.addWidget(self.category_list, 1)
        workspace.addWidget(category_panel)

        self.table = QTableWidget(0, 8)
        self.table.setObjectName("dataTable")
        self.table.setHorizontalHeaderLabels(["勾选", "封面", "文件名", "备注", "类型", "大小(MB)", "分类", "操作"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 56)
        self.table.setColumnWidth(1, 112)
        self.table.setColumnWidth(2, 260)
        self.table.setColumnWidth(3, 190)
        self.table.setColumnWidth(4, 80)
        self.table.setColumnWidth(5, 90)
        self.table.setColumnWidth(6, 180)
        self.table.setColumnWidth(7, 76)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.cellDoubleClicked.connect(self.preview_row)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.open_menu)
        content_panel = QWidget()
        content_layout = QVBoxLayout(content_panel)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(10)
        content_layout.addWidget(filters)
        content_layout.addWidget(self.table, 1)
        workspace.addWidget(content_panel)
        workspace.setStretchFactor(0, 0)
        workspace.setStretchFactor(1, 1)
        workspace.setSizes([190, 980])
        layout.addWidget(workspace, 1)
        self.refresh()

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = [url.toLocalFile() for url in event.mimeData().urls()]
        category = self._import_category()
        count = media_service.import_files(paths, category=category)
        QMessageBox.information(self, "导入素材", f"已导入 {count} 个素材到“{category}”")
        self.refresh()

    def refresh(self) -> None:
        self.refresh_categories()
        rows = self.filtered_rows()
        self.table.clearSpans()
        # 空态行会合并整行并在首列写入“暂无素材”。首个素材导入后若不清理
        # 旧单元格，勾选框所在列可能残留该文本。
        self.table.clearContents()
        self.table.setRowCount(len(rows))
        self.select_all.blockSignals(True)
        self.select_all.setChecked(False)
        self.select_all.blockSignals(False)
        for row_idx, row in enumerate(rows):
            check = QCheckBox()
            check.setProperty("media_id", row["id"])
            check.stateChanged.connect(self.update_selected_count)
            self.table.setCellWidget(row_idx, 0, check)
            self.table.setCellWidget(row_idx, 1, self._cover(row))
            self.table.setItem(row_idx, 2, table_item(row["filename"]))
            self.table.setItem(row_idx, 3, table_item(row["remark"]))
            color = "#ea580c" if row["typeText"] == "视频" else "#0891b2"
            self.table.setItem(row_idx, 4, table_item(row["typeText"], color))
            self.table.setItem(row_idx, 5, table_item(row.get("filesize") or ""))
            category_item = table_item(row.get("mediaCategory") or "其他")
            category_item.setToolTip("素材分类由上传时选择；账号主体会自动出现在左侧分类中。")
            self.table.setItem(row_idx, 6, category_item)
            self.table.setCellWidget(row_idx, 7, self._actions(row))
            self.table.item(row_idx, 2).setData(Qt.ItemDataRole.UserRole, row)
            self.table.setRowHeight(row_idx, 72)
        if not rows:
            self.table.setRowCount(1)
            empty = table_item("暂无素材")
            empty.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(0, 0, empty)
            self.table.setSpan(0, 0, 1, self.table.columnCount())
            self.table.setRowHeight(0, 52)
        self.update_selected_count()

    def filtered_rows(self) -> list[dict]:
        keyword = self.search_input.text().strip().lower()
        type_text = self.type_filter.currentText()
        rows = []
        for row in media_service.list_media():
            if self.current_category != "全部" and row.get("mediaCategory") != self.current_category:
                continue
            if type_text != "全部素材" and row["typeText"] != type_text:
                continue
            searchable = f"{row.get('filename') or ''} {row.get('remark') or ''} {row.get('storedPath') or ''}".lower()
            if keyword and keyword not in searchable:
                continue
            rows.append(row)
        return rows

    def refresh_categories(self) -> None:
        selected = self.current_category
        categories = media_service.list_categories()
        names = {item["name"] for item in categories}
        if selected not in names:
            selected = "全部"
        self.category_list.blockSignals(True)
        self.category_list.clear()
        selected_item = None
        for category in categories:
            item = QListWidgetItem(f"{category['name']}  ·  {category['count']}")
            item.setData(Qt.ItemDataRole.UserRole, category["name"])
            self.category_list.addItem(item)
            if category["name"] == selected:
                selected_item = item
        if selected_item:
            self.category_list.setCurrentItem(selected_item)
        self.category_list.blockSignals(False)
        self.current_category = selected
        self.add_btn.setText(f"导入到“{self._import_category()}”")

    def category_changed(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if current is None:
            return
        self.current_category = str(current.data(Qt.ItemDataRole.UserRole) or "全部")
        self.refresh()

    def _import_category(self) -> str:
        return self.current_category if self.current_category != "全部" else "其他"

    def _cover(self, row: dict) -> QLabel:
        label = QLabel(row["typeText"])
        label.setObjectName("mediaThumbnail")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setMinimumSize(96, 64)
        path = media_service.cover_display_path(row)
        if path:
            pix = QPixmap(path)
            if not pix.isNull():
                label.setPixmap(pix.scaled(96, 64, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        return label

    def _actions(self, row: dict) -> QWidget:
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(5, 19, 5, 19)
        menu = self._build_media_context_menu(row, parent=box)
        action_btn = button("操作", variant="secondary", compact=True)
        action_btn.clicked.connect(
            lambda _checked=False, b=action_btn, m=menu: m.exec(
                b.mapToGlobal(b.rect().bottomLeft())
            )
        )
        layout.addWidget(action_btn)
        return box

    def preview_row(self, row_index: int, _column: int) -> None:
        item = self.table.item(row_index, 2)
        row = item.data(Qt.ItemDataRole.UserRole) if item else None
        if row:
            self.preview(row)

    def row_data(self) -> dict | None:
        item = self.table.item(self.table.currentRow(), 2)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def open_menu(self, pos) -> None:
        index = self.table.indexAt(pos)
        if index.isValid():
            self.table.selectRow(index.row())
        row = self.row_data()
        if not row:
            return
        menu = self._build_media_context_menu(row)
        menu.exec(self.table.mapToGlobal(pos))

    def _build_media_context_menu(
        self,
        row: dict,
        *,
        parent: QWidget | None = None,
    ) -> QMenu:
        return build_media_context_menu(
            parent or self,
            row,
            preview=self.preview,
            rename=self.rename,
            edit_remark=self.edit_remark,
            refresh_cover=self.refresh_one_cover,
            open_folder=self.open_folder,
            delete=self.delete_one,
        )

    def import_dialog(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "选择素材")
        if paths:
            category = self._import_category()
            count = media_service.import_files(paths, category=category)
            QMessageBox.information(self, "导入素材", f"已导入 {count} 个素材到“{category}”")
            self.refresh()

    def toggle_all(self) -> None:
        checked = self.select_all.isChecked()
        for row in range(self.table.rowCount()):
            widget = self.table.cellWidget(row, 0)
            if isinstance(widget, QCheckBox):
                widget.blockSignals(True)
                widget.setChecked(checked)
                widget.blockSignals(False)
        self.update_selected_count()

    def update_selected_count(self) -> None:
        count = len(self.selected_ids())
        total = sum(
            isinstance(self.table.cellWidget(row, 0), QCheckBox)
            for row in range(self.table.rowCount())
        )
        self.selected_label.setText(f"已勾选 {count} / {total}")
        self.select_all.blockSignals(True)
        self.select_all.setChecked(bool(total and count == total))
        self.select_all.blockSignals(False)

    def selected_ids(self) -> list[int]:
        ids = []
        for row in range(self.table.rowCount()):
            widget = self.table.cellWidget(row, 0)
            if isinstance(widget, QCheckBox) and widget.isChecked():
                ids.append(int(widget.property("media_id")))
        return ids

    def delete_selected(self) -> None:
        ids = self.selected_ids()
        if not ids:
            QMessageBox.information(self, "删除素材", "请先勾选素材")
            return
        if QMessageBox.question(self, "删除素材", f"确定删除 {len(ids)} 个素材？") == QMessageBox.StandardButton.Yes:
            media_service.delete_media(ids)
            self.refresh()

    def refresh_selected_covers(self) -> None:
        ids = self.selected_ids()
        if not ids:
            QMessageBox.information(self, "刷新封面", "请先勾选需要刷新封面的视频素材")
            return
        count = media_service.refresh_covers(ids)
        QMessageBox.information(self, "刷新封面", f"已刷新 {count} 个视频封面")
        self.refresh()

    def refresh_one_cover(self, row: dict) -> None:
        count = media_service.refresh_covers([int(row["id"])])
        QMessageBox.information(self, "刷新封面", "封面刷新完成" if count else "当前素材不是视频，或暂时无法生成封面")
        self.refresh()

    def preview(self, row: dict) -> None:
        path = Path(row["storedPath"])
        if not path.exists():
            QMessageBox.warning(self, "预览素材", "文件不存在")
            return
        open_path(path)

    def open_folder(self, row: dict) -> None:
        path = Path(row["storedPath"])
        folder = path.parent if path.parent.exists() else ROOT_DIR
        reveal_in_folder(folder)

    def rename(self, row: dict) -> None:
        text, ok = QInputDialog.getText(self, "重命名素材", "文件名：", text=row["filename"])
        if ok and text.strip():
            media_service.rename_media(row["id"], text)
            self.refresh()

    def edit_remark(self, row: dict) -> None:
        text, ok = QInputDialog.getText(self, "编辑备注", "备注：", text=row.get("remark") or "")
        if ok:
            media_service.update_remark(row["id"], text)
            self.refresh()

    def delete_one(self, row: dict) -> None:
        if QMessageBox.question(self, "删除素材", f"确定删除 {row['filename']}？") == QMessageBox.StandardButton.Yes:
            media_service.delete_media([row["id"]])
            self.refresh()
