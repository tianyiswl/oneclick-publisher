"""Local Channels video batch history backed only by BatchQueue."""
from PyQt6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton,
                             QTableWidget, QTableWidgetItem, QVBoxLayout, QHeaderView)

from app_core.video_batch_service import list_batches, batch_details, cancel_batch


class VideoBatchHistoryDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('视频号本地批次')
        self.resize(1100, 700)
        self.rows = []
        layout = QVBoxLayout(self)
        heading = QHBoxLayout()
        heading.addWidget(QLabel('视频号本地批次（最近 100 条）'))
        heading.addStretch()
        refresh = QPushButton('刷新'); refresh.clicked.connect(self.refresh); heading.addWidget(refresh)
        self.cancel_button = QPushButton('取消选中批次的待执行项')
        self.cancel_button.clicked.connect(self.cancel_pending); heading.addWidget(self.cancel_button)
        layout.addLayout(heading)
        self.error = QLabel(''); self.error.setWordWrap(True); layout.addWidget(self.error)
        self.batch_table = QTableWidget(0, 4)
        self.batch_table.setHorizontalHeaderLabels(['批次 ID', '账号', '条数', '状态'])
        self.batch_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.batch_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.batch_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.batch_table.currentCellChanged.connect(lambda row, *_: self.show_detail(row))
        layout.addWidget(self.batch_table, 1)
        self.notice = QLabel('“平台定时受理”只表示平台接受排期，不代表内容已公开成功。')
        self.notice.setWordWrap(True); layout.addWidget(self.notice)
        self.detail_identity = QLabel('当前详情：未选择批次')
        self.detail_identity.setWordWrap(True); layout.addWidget(self.detail_identity)
        self.item_table = QTableWidget(0, 7)
        self.item_table.setHorizontalHeaderLabels(['文件', '短标题', '排期', '状态', '阶段', '错误', '回执'])
        self.item_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.item_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.item_table, 2)
        self.refresh()

    def refresh(self):
        try:
            rows = list_batches()
        except Exception as exc:
            self.error.setText(f'刷新失败，已保留上次显示：{exc}')
            return
        self.error.setText(''); self.rows = rows; self.batch_table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            values = [row['batchId'], row.get('accountDisplay') or f"账号 ID {row['accountId']}", str(row['itemCount']), row['statusLabel']]
            for col, value in enumerate(values): self.batch_table.setItem(index, col, QTableWidgetItem(value))
        if rows:
            self.batch_table.setCurrentCell(0, 0); self.show_detail(0)
        else: self.item_table.setRowCount(0)

    def show_detail(self, row):
        if not 0 <= row < len(self.rows): return
        try: detail = batch_details(self.rows[row]['batchId'])
        except Exception as exc:
            self.error.setText(f'详情读取失败，已保留上次显示：{exc}'); return
        self.error.setText('')
        reason = ' / '.join(filter(None, [detail.get('errorCode'), detail.get('errorText')]))
        self.detail_identity.setText(
            f"当前详情：{detail['batchId']} · 批次阶段：{detail.get('stageLabel') or '未知阶段'}"
            + (f" · 批次原因：{reason}" if reason else '')
        )
        self.item_table.setRowCount(len(detail['items']))
        for index, item in enumerate(detail['items']):
            receipt = item.get('receipt') or {}
            receipt_text = ' / '.join(str(receipt.get(k)) for k in ('platformPostId', 'postUrl', 'scheduledAt') if receipt.get(k))
            error = ' / '.join(filter(None, [item.get('errorCode'), item.get('errorText')]))
            values = [item['fileName'], item['title'], item.get('scheduledAt') or '顺序立即', item['statusLabel'], item.get('stageLabel') or '未知阶段', error, receipt_text]
            for col, value in enumerate(values): self.item_table.setItem(index, col, QTableWidgetItem(str(value)))

    def cancel_pending(self):
        row = self.batch_table.currentRow()
        if not 0 <= row < len(self.rows):
            QMessageBox.information(self, '取消批次', '请先选择一个本地批次。'); return
        try: cancel_batch(self.rows[row]['batchId'])
        except Exception as exc:
            self.error.setText(f'取消失败，未改变当前显示：{exc}'); return
        self.refresh()
