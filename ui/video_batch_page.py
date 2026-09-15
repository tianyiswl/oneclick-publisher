"""视频号批量视频内容准备。正式入口须在平台回执接通后开放。"""
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QListWidget, QListWidgetItem, QLineEdit, QTextEdit, QCheckBox,
    QFormLayout, QWidget, QFileDialog, QMessageBox, QSpinBox, QTableWidget,
    QTableWidgetItem, QHeaderView)
from app_core import account_service, media_service
from app_core.video_batch_draft_service import save_draft, load_draft
from app_core.video_batch_service import check_batch, prepare_and_store, batch_status, cancel_batch
from .background_task import BackgroundTaskRunner


class VideoBatchDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('视频号批量视频')
        self.resize(1180, 780)
        self.items = []
        self.current_id = None
        self.loading = False
        self.snapshot = None
        self.batch_id = None
        self.runner = BackgroundTaskRunner(self)
        root = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel('视频号 · 一个账号，多条视频'))
        self.accounts = QComboBox()
        for row in account_service.list_accounts():
            if row.get('type') == 2:
                self.accounts.addItem(str(row.get('userName') or row.get('profileName') or row['id']), row['id'])
        top.addWidget(self.accounts)
        for text, callback in [('保存内容', self.save), ('恢复内容', self.restore), ('清空填写', self.clear)]:
            button = QPushButton(text)
            button.clicked.connect(callback)
            top.addWidget(button)
        root.addLayout(top)
        columns = QHBoxLayout()
        root.addLayout(columns, 1)
        left = QVBoxLayout()
        columns.addLayout(left, 1)
        left.addWidget(QLabel('视频素材（最多20条）'))
        self.videos = QListWidget()
        self.videos.currentRowChanged.connect(self.select_item)
        left.addWidget(self.videos)
        for labels in [[('素材管理', self.pick_library), ('本机添加', self.pick_files)],
                       [('上移', lambda: self.move(-1)), ('下移', lambda: self.move(1)), ('移除', self.remove)]]:
            line = QHBoxLayout()
            for text, callback in labels:
                button = QPushButton(text)
                button.clicked.connect(callback)
                line.addWidget(button)
            left.addLayout(line)
        middle = QFormLayout()
        columns.addLayout(middle, 2)
        self.overrides = {}
        self.editors = {}
        for key, label in [('title', '短标题'), ('body', '正文'), ('tags', '话题（逗号分隔）')]:
            box = QCheckBox('独立设置' + label)
            editor = QTextEdit() if key == 'body' else QLineEdit()
            middle.addRow(box)
            middle.addRow(editor)
            box.toggled.connect(lambda checked, k=key: self.toggle_override(k, checked))
            editor.textChanged.connect(self.capture_item)
            self.overrides[key], self.editors[key] = box, editor
        self.cover = QLineEdit()
        self.cover.setPlaceholderText('可选：封面文件绝对路径')
        self.cover.textChanged.connect(self.capture_item)
        middle.addRow('当前视频封面', self.cover)
        cover_button = QPushButton('选择封面')
        cover_button.clicked.connect(self.pick_cover)
        middle.addRow(cover_button)
        self.item_schedule = QLineEdit()
        self.item_schedule.setPlaceholderText('留空跟随排期；YYYY-MM-DD HH:mm')
        self.item_schedule.textChanged.connect(self.capture_item)
        middle.addRow('独立发布时间', self.item_schedule)
        common = QFormLayout()
        columns.addLayout(common, 2)
        self.defaults = {}
        for key, label in [('title', '通用短标题（1–10字）'), ('body', '通用正文'), ('tags', '通用话题（逗号分隔）')]:
            editor = QTextEdit() if key == 'body' else QLineEdit()
            self.defaults[key] = editor
            editor.textChanged.connect(self.common_changed)
            common.addRow(label, editor)
        self.mode = QComboBox()
        for label, value in [('顺序立即发布', 'immediate'), ('起始时间＋间隔', 'interval'), ('每天固定时段', 'daily')]:
            self.mode.addItem(label, value)
        self.start = QLineEdit((datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d 09:00'))
        self.interval = QSpinBox()
        self.interval.setRange(1, 10080)
        self.interval.setValue(30)
        self.times = QLineEdit('09:00,18:00')
        common.addRow('排期方式', self.mode)
        common.addRow('起始日期时间', self.start)
        common.addRow('间隔（分钟）', self.interval)
        common.addRow('每日时段', self.times)
        self.result = QTableWidget(0, 3)
        self.result.setHorizontalHeaderLabels(['视频', '最终短标题', '北京时间'])
        self.result.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.result.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        root.addWidget(self.result)
        self.status = QLabel('准备内容只保存在本机。平台原生定时能力待当前账号验证。')
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        check = QPushButton('检查内容与排期')
        check.clicked.connect(self.check)
        root.addWidget(check)
        actions = QHBoxLayout()
        self.prepare_button = QPushButton('生成本地待执行批次')
        self.prepare_button.clicked.connect(self.prepare_local_batch)
        actions.addWidget(self.prepare_button)
        self.cancel_button = QPushButton('取消本地待执行项')
        self.cancel_button.clicked.connect(self.cancel_local_batch)
        actions.addWidget(self.cancel_button)
        root.addLayout(actions)
        self.formal_gate = QLabel('正式授权与启动暂不可用：当前页面、官方话题实体和唯一回执尚未验证。')
        self.formal_gate.setWordWrap(True)
        root.addWidget(self.formal_gate)

    def text(self, widget):
        return widget.toPlainText() if isinstance(widget, QTextEdit) else widget.text()

    def set_text(self, widget, text):
        widget.setPlainText(text) if isinstance(widget, QTextEdit) else widget.setText(text)

    def content(self):
        return {key: self.parse(key, self.text(editor)) for key, editor in self.defaults.items()}

    def parse(self, key, text):
        return [t.strip() for t in text.replace('，', ',').split(',') if t.strip()] if key == 'tags' else text

    def current(self):
        return next((i for i in self.items if i['itemId'] == self.current_id), None)

    def capture_item(self):
        if self.loading:
            return
        item = self.current()
        if item:
            item['overrides'] = {k: self.parse(k, self.text(self.editors[k])) for k, box in self.overrides.items() if box.isChecked()}
            item['coverPath'] = self.cover.text()
            if self.item_schedule.text().strip():
                item['scheduledAt'] = self.item_schedule.text().strip()
            else:
                item.pop('scheduledAt', None)
        self.snapshot = None

    def toggle_override(self, key, checked):
        self.editors[key].setEnabled(checked)
        self.capture_item()
        if not self.loading:
            self.select_item(self.videos.currentRow())

    def common_changed(self):
        if not self.loading:
            self.snapshot = None
            self.select_item(self.videos.currentRow())

    def select_item(self, row):
        self.loading = True
        try:
            item = self.items[row] if 0 <= row < len(self.items) else None
            self.current_id = item['itemId'] if item else None
            for key, editor in self.editors.items():
                override = item.get('overrides', {}) if item else {}
                value = override.get(key, self.content()[key])
                self.overrides[key].setChecked(key in override)
                self.overrides[key].setEnabled(item is not None)
                editor.setEnabled(item is not None and key in override)
                self.set_text(editor, ','.join(value) if key == 'tags' else value)
            self.cover.setText(item.get('coverPath', '') if item else '')
            self.item_schedule.setText(item.get('scheduledAt', '') if item else '')
        finally:
            self.loading = False

    def refresh_list(self, selected=0):
        self.videos.blockSignals(True)
        self.videos.clear()
        for i, item in enumerate(self.items):
            self.videos.addItem(f"{i + 1}. {Path(item['path']).name}")
        self.videos.blockSignals(False)
        self.videos.setCurrentRow(selected)
        self.select_item(selected)

    def add_paths(self, paths):
        known = {i['path'] for i in self.items}
        fresh = [p for p in dict.fromkeys(paths) if p not in known]
        if len(self.items) + len(fresh) > 20:
            QMessageBox.warning(self, '视频数量', '每批最多20条，请减少选择')
            return
        self.items.extend({'itemId': uuid4().hex, 'path': p, 'overrides': {}} for p in fresh)
        self.refresh_list()

    def pick_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, '添加视频', '', '视频 (*.mp4 *.mov *.mkv *.avi)')
        self.add_paths(paths)

    def pick_library(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('选择素材管理中的视频')
        layout = QVBoxLayout(dialog)
        listing = QListWidget()
        for media in media_service.list_media():
            if media.get('typeText') == '视频' and Path(media.get('storedPath') or '').is_file():
                row = QListWidgetItem(media.get('filename') or Path(media['storedPath']).name)
                row.setData(Qt.ItemDataRole.UserRole, media['storedPath'])
                row.setCheckState(Qt.CheckState.Unchecked)
                listing.addItem(row)
        layout.addWidget(listing)
        for label, state in [('全选', Qt.CheckState.Checked), ('取消全选', Qt.CheckState.Unchecked)]:
            button = QPushButton(label)
            button.clicked.connect(lambda _, s=state: [listing.item(i).setCheckState(s) for i in range(listing.count())])
            layout.addWidget(button)
        add = QPushButton('加入批次')
        add.clicked.connect(dialog.accept)
        layout.addWidget(add)
        if dialog.exec():
            self.add_paths([listing.item(i).data(Qt.ItemDataRole.UserRole) for i in range(listing.count()) if listing.item(i).checkState() == Qt.CheckState.Checked])

    def move(self, delta):
        row = self.videos.currentRow()
        target = row + delta
        if 0 <= row < len(self.items) and 0 <= target < len(self.items):
            self.items[row], self.items[target] = self.items[target], self.items[row]
            self.refresh_list(target)

    def remove(self):
        row = self.videos.currentRow()
        if 0 <= row < len(self.items):
            self.items.pop(row)
            self.refresh_list(min(row, len(self.items) - 1))

    def pick_cover(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择封面', '', '图片 (*.png *.jpg *.jpeg)')
        if path:
            self.cover.setText(path)

    def request(self):
        self.capture_item()
        mode = self.mode.currentData()
        policy = {'mode': mode}
        if mode == 'interval':
            policy.update(start=self.start.text().strip(), intervalMinutes=self.interval.value())
        elif mode == 'daily':
            policy.update(startDate=self.start.text().strip().split(' ')[0], times=self.parse('tags', self.times.text()))
        return {'platform': '视频号', 'accountId': self.accounts.currentData(), 'defaults': self.content(),
                'items': deepcopy(self.items), 'schedulePolicy': policy}

    def save(self):
        try:
            save_draft(self.request())
            self.status.setText('批次内容已保存到本机')
        except (ValueError, OSError) as exc:
            self.status.setText(str(exc))

    def restore(self):
        try:
            draft = load_draft()
            if draft is None:
                self.status.setText('没有保存内容')
                return
            self.loading = True
            self.accounts.setCurrentIndex(self.accounts.findData(draft.get('accountId')))
            for key, editor in self.defaults.items():
                value = draft.get('defaults', {}).get(key, [] if key == 'tags' else '')
                self.set_text(editor, ','.join(value) if key == 'tags' else value)
            self.items = draft.get('items', [])
            policy = draft.get('schedulePolicy', {})
            self.mode.setCurrentIndex(self.mode.findData(policy.get('mode', 'immediate')))
            self.start.setText(policy.get('start') or (policy.get('startDate', '') + ' 09:00'))
            self.interval.setValue(policy.get('intervalMinutes', 30))
            self.times.setText(','.join(policy.get('times', ['09:00', '18:00'])))
            self.loading = False
            self.refresh_list()
            self.status.setText('已恢复；提交前需重新检查文件与排期')
        except (ValueError, OSError) as exc:
            self.status.setText(str(exc))
        finally:
            self.loading = False

    def clear(self):
        self.items = []
        self.refresh_list()
        for editor in self.defaults.values():
            editor.clear()
        self.result.setRowCount(0)
        self.status.setText('当前填写已清空，已保存草稿和素材仍保留')

    def prepare_local_batch(self):
        if self._busy():
            return
        request = self.request()
        self.status.setText('正在复核并保存不可变批次…')
        def succeeded(result):
            self.batch_id = result['batchId']
            current = batch_status(self.batch_id)
            self.status.setText(f"本地批次 {self.batch_id} 已准备，{len(current['items'])} 条待执行；正式入口仍关闭")
        self.runner.run('batch-prepare', lambda: prepare_and_store(request),
                        on_success=succeeded, on_error=self.status.setText)

    def cancel_local_batch(self):
        if not self.batch_id:
            self.status.setText('当前没有已生成的本地批次')
            return
        try:
            result = cancel_batch(self.batch_id)
            self.status.setText(f"本地批次 {self.batch_id} 状态：{result['status']}")
        except (ValueError, OSError) as exc:
            self.status.setText(str(exc))

    def check(self):
        if self._busy():
            return
        try:
            request = self.request()
        except (ValueError, OSError) as exc:
            self.status.setText(str(exc))
            return
        self.status.setText('正在检查文件与排期…')
        self.snapshot = None

        def succeeded(result):
            if self.request() != request:
                self.status.setText('检查期间内容已变化，请重新检查')
                return
            self.snapshot = result['snapshot']
            self.result.setRowCount(len(self.snapshot['items']))
            for row, item in enumerate(self.snapshot['items']):
                for col, text in enumerate([Path(item['path']).name, item['content']['title'], item['scheduledAt'] or '顺序立即发布']):
                    self.result.setItem(row, col, QTableWidgetItem(text))
            self.status.setText(result['message'])

        def failed(message):
            self.snapshot = None
            self.result.setRowCount(0)
            self.status.setText(message)

        self.runner.run('batch-check', lambda: check_batch(request), on_success=succeeded, on_error=failed)

    def _busy(self):
        return any(self.runner.is_running(name) for name in ('batch-check', 'batch-prepare'))

    def reject(self):
        if self._busy():
            self.status.setText('正在检查文件，请等待检查结束后关闭')
            return
        super().reject()

    def closeEvent(self, event):
        if self._busy():
            self.status.setText('正在检查文件，请等待检查结束后关闭')
            event.ignore()
        else:
            super().closeEvent(event)
