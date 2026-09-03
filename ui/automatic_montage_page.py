# -*- coding: utf-8 -*-
"""轻量自动混剪与人工文案变体工作页。"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
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
    QProgressBar,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app_core import media_service
from app_core.montage_models import MontageFailure, MontageRequest
from app_core.montage_service import MontageBatchResult, run_montage_batch

from .background_task import BackgroundTaskRunner
from .common import COLORS, button, table_item
from .platform_open import open_path, reveal_in_folder


class ImeAwarePlainTextEdit(QPlainTextEdit):
    """中文组词期间隐藏占位文字，避免与输入法候选栏重叠。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._configured_placeholder = ""
        self._ime_composing = False

    def setPlaceholderText(self, text: str) -> None:  # noqa: N802
        self._configured_placeholder = str(text)
        if not self._ime_composing:
            super().setPlaceholderText(self._configured_placeholder)

    def inputMethodEvent(self, event) -> None:  # noqa: N802
        self._ime_composing = bool(event.preeditString())
        super().setPlaceholderText("" if self._ime_composing else self._configured_placeholder)
        super().inputMethodEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        self._ime_composing = False
        super().setPlaceholderText(self._configured_placeholder)
        super().focusOutEvent(event)


class AutomaticMontagePage(QWidget):
    """在发布之前生成可人工检查的本地混剪结果。"""

    TASK_KEY = "automatic-montage"

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        batch_service: Callable[..., MontageBatchResult] = run_montage_batch,
        task_runner: BackgroundTaskRunner | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("pageRoot")
        self._batch_service = batch_service
        self.runner = task_runner or BackgroundTaskRunner(self)
        self._local_paths: list[Path] = []
        self._last_batch_dir: Path | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)
        root.addLayout(self._build_heading())

        columns = QHBoxLayout()
        columns.setSpacing(12)
        columns.addWidget(self._build_sources_panel(), 4)
        columns.addWidget(self._build_settings_panel(), 3)
        columns.addWidget(self._build_copy_panel(), 4)
        root.addLayout(columns, 3)

        root.addWidget(self._build_progress_panel())
        root.addWidget(self._build_results_panel(), 2)
        self.refresh()

    @staticmethod
    def _panel() -> tuple[QFrame, QVBoxLayout]:
        panel = QFrame()
        panel.setProperty("subPanel", True)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        return panel, layout

    def _build_heading(self) -> QHBoxLayout:
        row = QHBoxLayout()
        titles = QVBoxLayout()
        title = QLabel("自动混剪")
        title.setObjectName("pageTitle")
        subtitle = QLabel("多条素材按固定镜头时长混剪成不同竖屏视频；只在本地生成，检查后再进入发布。")
        subtitle.setProperty("role", "caption")
        subtitle.setWordWrap(True)
        titles.addWidget(title)
        titles.addWidget(subtitle)
        row.addLayout(titles, 1)
        self.refresh_button = button("刷新素材", variant="secondary")
        self.refresh_button.clicked.connect(self.refresh)
        row.addWidget(self.refresh_button)
        self.open_output_button = button("打开输出目录", variant="secondary")
        self.open_output_button.setEnabled(False)
        self.open_output_button.clicked.connect(self.open_output_directory)
        row.addWidget(self.open_output_button)
        self.start_button = button("开始生成", variant="primary")
        self.start_button.clicked.connect(self.start_generation)
        row.addWidget(self.start_button)
        return row

    def _build_sources_panel(self) -> QFrame:
        panel, layout = self._panel()
        title = QLabel("视频素材（1–50 个）")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        hint = QLabel("默认读取素材管理中的本地视频，也可临时添加，不会改动源文件。")
        hint.setProperty("role", "caption")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.source_list = QListWidget()
        self.source_list.setAlternatingRowColors(True)
        self.source_list.itemChanged.connect(self._on_source_selection_changed)
        layout.addWidget(self.source_list, 1)
        self.source_count = QLabel("已选择 0 / 0")
        self.source_count.setProperty("role", "caption")
        layout.addWidget(self.source_count)
        actions = QHBoxLayout()
        self.add_sources_button = button("从本机添加", variant="primary", compact=True)
        self.add_sources_button.clicked.connect(self.add_local_sources)
        actions.addWidget(self.add_sources_button)
        actions.addStretch()
        self.select_all_button = button("全选", variant="secondary", compact=True)
        self.select_all_button.clicked.connect(self.select_all_sources)
        actions.addWidget(self.select_all_button)
        self.clear_selection_button = button("取消全选", variant="secondary", compact=True)
        self.clear_selection_button.clicked.connect(self.clear_source_selection)
        actions.addWidget(self.clear_selection_button)
        layout.addLayout(actions)
        return panel

    def _build_settings_panel(self) -> QFrame:
        panel, layout = self._panel()
        title = QLabel("混剪设置")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)

        self.output_count = QSpinBox()
        self.output_count.setRange(1, 50)
        self.output_count.setValue(5)
        self.output_count.setSuffix(" 条")
        grid.addWidget(QLabel("生成数量"), 0, 0)
        grid.addWidget(self.output_count, 0, 1)

        self.target_duration = QSpinBox()
        self.target_duration.setRange(5, 180)
        self.target_duration.setValue(30)
        self.target_duration.setSuffix(" 秒")
        self.target_duration_label = QLabel("每条总时长")
        self.target_duration_follow_label = QLabel("读回语音 + 300ms（5–180 秒）")
        self.target_duration_follow_label.setProperty("role", "caption")
        grid.addWidget(self.target_duration_label, 1, 0)
        grid.addWidget(self.target_duration, 1, 1)
        grid.addWidget(self.target_duration_follow_label, 1, 1)

        self.clip_duration = QDoubleSpinBox()
        self.clip_duration.setRange(0.5, 10.0)
        self.clip_duration.setSingleStep(0.5)
        self.clip_duration.setDecimals(1)
        self.clip_duration.setValue(2.0)
        self.clip_duration.setSuffix(" 秒")
        grid.addWidget(QLabel("每个镜头"), 2, 0)
        grid.addWidget(self.clip_duration, 2, 1)

        self.audio_mode = QComboBox()
        self.audio_mode.addItem("静音", "mute")
        self.audio_mode.addItem("保留环境原声（仅无人声素材）", "source")
        self.audio_mode.addItem("系统自动配音", "narration")
        grid.addWidget(QLabel("声音"), 3, 0)
        grid.addWidget(self.audio_mode, 3, 1)

        self.seed = QSpinBox()
        self.seed.setRange(0, 2_147_483_647)
        self.seed.setValue(20260903)
        self.seed.setToolTip("同样的素材、设置和随机种子会生成相同剪辑计划")
        grid.addWidget(QLabel("随机种子"), 4, 0)
        grid.addWidget(self.seed, 4, 1)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

        self.source_audio_confirmation = QCheckBox("我确认所选素材不含连续讲话")
        self.source_audio_confirmation.setObjectName("sourceAudioConfirmation")
        self.source_audio_confirmation.setToolTip(
            "固定时长切片无法保证口播语句完整；有人声口播时请使用静音混剪"
        )
        layout.addWidget(self.source_audio_confirmation)
        self.narration_panel = QFrame()
        narration_layout = QVBoxLayout(self.narration_panel)
        narration_layout.setContentsMargins(0, 0, 0, 0)
        narration_layout.addWidget(QLabel("配音文案"))
        self.narration_text = ImeAwarePlainTextEdit()
        self.narration_text.setPlaceholderText("输入需要完整朗读的中文解说；整批视频共用这一条配音")
        self.narration_text.setMaximumHeight(120)
        narration_layout.addWidget(self.narration_text)
        narration_hint = QLabel("系统会移除素材原声，视频时长由配音决定。")
        narration_hint.setProperty("role", "caption")
        narration_layout.addWidget(narration_hint)
        layout.addWidget(self.narration_panel)
        self.audio_mode.currentIndexChanged.connect(self._sync_audio_mode_controls)
        self._sync_audio_mode_controls()

        self.allow_reuse = QCheckBox("素材不足时允许受控复用")
        self.allow_reuse.setToolTip("默认不复用同一时间段；勾选后按使用次数最少优先复用")
        layout.addWidget(self.allow_reuse)
        safety = QLabel("默认严格不重复。素材不够时会先告诉你最多可生成多少条，不会偷偷复用。")
        safety.setWordWrap(True)
        safety.setProperty("role", "caption")
        layout.addWidget(safety)
        layout.addStretch()
        return panel

    def _build_copy_panel(self) -> QFrame:
        panel, layout = self._panel()
        title = QLabel("文案变体（可不填）")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        hint = QLabel("用 [今天|这次] 这样的候选组生成不同表达；系统不会擅自改写产品事实。")
        hint.setProperty("role", "caption")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addWidget(QLabel("标题模板"))
        self.title_template = QLineEdit()
        self.title_template.setPlaceholderText("例如：[今天|这次]带你看[新品|新款]")
        layout.addWidget(self.title_template)
        layout.addWidget(QLabel("正文模板"))
        self.body_template = ImeAwarePlainTextEdit()
        self.body_template.setPlaceholderText("例如：重点看看[做工|细节]，喜欢可以收藏。")
        layout.addWidget(self.body_template, 1)
        return panel

    def _build_progress_panel(self) -> QFrame:
        panel, layout = self._panel()
        row = QHBoxLayout()
        self.status_label = QLabel("等待选择素材")
        self.status_label.setProperty("role", "caption")
        row.addWidget(self.status_label, 1)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        row.addWidget(self.progress_bar, 2)
        layout.addLayout(row)
        return panel

    def _build_results_panel(self) -> QFrame:
        panel, layout = self._panel()
        title = QLabel("生成结果（双击打开视频）")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        self.result_table = QTableWidget(0, 6)
        self.result_table.setHorizontalHeaderLabels(["结果", "标题", "正文", "时长", "剪辑指纹", "文件"])
        header = self.result_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.result_table.setColumnWidth(0, 78)
        self.result_table.setColumnWidth(3, 82)
        self.result_table.setColumnWidth(4, 150)
        self.result_table.verticalHeader().setVisible(False)
        self.result_table.setAlternatingRowColors(True)
        self.result_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.result_table.cellDoubleClicked.connect(self.open_result_video)
        layout.addWidget(self.result_table, 1)
        return panel

    def refresh(self) -> None:
        checked = set(self.selected_source_paths()) if hasattr(self, "source_list") else set()
        records: list[tuple[Path, str]] = []
        seen: set[Path] = set()
        for row in media_service.list_media():
            if row.get("typeText") != "视频":
                continue
            path = Path(str(row.get("storedPath") or "")).expanduser().resolve()
            if not path.is_file() or path in seen:
                continue
            seen.add(path)
            category = str(row.get("mediaCategory") or "其他")
            filename = str(row.get("filename") or path.name)
            records.append((path, f"{filename}  ·  {category}"))
        for path in self._local_paths:
            resolved = path.resolve()
            if resolved.is_file() and resolved not in seen:
                seen.add(resolved)
                records.append((resolved, f"{resolved.name}  ·  本机临时"))

        self.source_list.blockSignals(True)
        self.source_list.clear()
        for path, label in records:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if str(path) in checked else Qt.CheckState.Unchecked)
            item.setToolTip(str(path))
            self.source_list.addItem(item)
        self.source_list.blockSignals(False)
        self._on_source_selection_changed()

    def selected_source_paths(self) -> list[str]:
        selected: list[str] = []
        for index in range(self.source_list.count()):
            item = self.source_list.item(index)
            if item.checkState() == Qt.CheckState.Checked:
                selected.append(str(item.data(Qt.ItemDataRole.UserRole)))
        return selected

    def _update_source_count(self, _item: QListWidgetItem | None = None) -> None:
        self.source_count.setText(f"已选择 {len(self.selected_source_paths())} / {self.source_list.count()}")

    def _on_source_selection_changed(self, _item: QListWidgetItem | None = None) -> None:
        self._update_source_count()
        if (
            hasattr(self, "source_audio_confirmation")
            and self.audio_mode.currentData() == "source"
        ):
            self.source_audio_confirmation.setChecked(False)

    def _sync_audio_mode_controls(self, _index: int | None = None) -> None:
        mode = self.audio_mode.currentData()
        source_mode = mode == "source"
        narration_mode = mode == "narration"
        self.source_audio_confirmation.setVisible(source_mode)
        if not source_mode:
            self.source_audio_confirmation.setChecked(False)
        self.narration_panel.setVisible(narration_mode)
        self.target_duration.setVisible(not narration_mode)
        self.target_duration_follow_label.setVisible(narration_mode)

    def select_all_sources(self) -> None:
        self.source_list.blockSignals(True)
        for index in range(self.source_list.count()):
            state = Qt.CheckState.Checked if index < 50 else Qt.CheckState.Unchecked
            self.source_list.item(index).setCheckState(state)
        self.source_list.blockSignals(False)
        self._on_source_selection_changed()

    def clear_source_selection(self) -> None:
        self.source_list.blockSignals(True)
        for index in range(self.source_list.count()):
            self.source_list.item(index).setCheckState(Qt.CheckState.Unchecked)
        self.source_list.blockSignals(False)
        self._on_source_selection_changed()

    def add_local_sources(self) -> None:
        filenames, _selected_filter = QFileDialog.getOpenFileNames(
            self,
            "选择视频素材",
            "",
            "视频文件 (*.mp4 *.mov *.mkv *.avi *.wmv *.flv *.webm)",
        )
        if not filenames:
            return
        added = [Path(filename).expanduser().resolve() for filename in filenames]
        self._local_paths = list(dict.fromkeys([*self._local_paths, *added]))[:50]
        self.refresh()
        added_text = {str(path) for path in added}
        for index in range(self.source_list.count()):
            item = self.source_list.item(index)
            if str(item.data(Qt.ItemDataRole.UserRole)) in added_text:
                item.setCheckState(Qt.CheckState.Checked)

    def build_request(self) -> MontageRequest:
        return MontageRequest.from_mapping(
            {
                "source_paths": self.selected_source_paths(),
                "output_count": self.output_count.value(),
                "target_duration_ms": self.target_duration.value() * 1000,
                "clip_duration_ms": round(self.clip_duration.value() * 1000),
                "allow_reuse": self.allow_reuse.isChecked(),
                "audio_mode": self.audio_mode.currentData(),
                "source_audio_confirmed": self.source_audio_confirmation.isChecked(),
                "seed": self.seed.value(),
                "title_template": self.title_template.text(),
                "body_template": self.body_template.toPlainText(),
                "narration_text": self.narration_text.toPlainText(),
            }
        )

    def start_generation(self) -> None:
        try:
            request = self.build_request()
        except MontageFailure as exc:
            QMessageBox.warning(self, "无法开始混剪", str(exc))
            return
        started = self.runner.run(
            self.TASK_KEY,
            with_progress=lambda report: self._batch_service(request, progress=report),
            on_started=self._on_started,
            on_progress=self._on_progress,
            on_success=self._on_success,
            on_error=self._on_error,
            on_finished=self._on_finished,
        )
        if not started:
            QMessageBox.information(self, "自动混剪", "当前批次仍在生成，请稍候")

    def _on_started(self) -> None:
        self.start_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.progress_bar.setValue(1)
        self.status_label.setStyleSheet("")
        self.status_label.setText("正在读取素材…")

    def _on_progress(self, event: object) -> None:
        if not isinstance(event, dict):
            return
        stage = event.get("stage")
        if stage == "probing":
            current = int(event.get("current") or 0)
            total = max(1, int(event.get("total") or 1))
            self.progress_bar.setValue(2 + round(current / total * 13))
            self.status_label.setText(f"正在读取素材 {current}/{total}：{event.get('filename') or ''}")
        elif stage == "narration_synthesizing":
            self.progress_bar.setValue(16)
            self.status_label.setText("正在生成系统配音…")
        elif stage == "narration_readback":
            duration = int(event.get("master_duration_ms") or 0) / 1000
            self.progress_bar.setValue(22)
            self.status_label.setText(f"配音已生成，成片时长 {duration:g} 秒；正在安排镜头…")
        elif stage == "planning":
            self.progress_bar.setValue(24)
            self.status_label.setText("正在安排不重复镜头…")
        elif stage == "rendering":
            current = int(event.get("output_index") or 1)
            total = max(1, int(event.get("output_count") or 1))
            self.progress_bar.setValue(25 + round((current - 1) / total * 65))
            self.status_label.setText(f"正在生成第 {current}/{total} 条视频…")
        elif stage == "output_completed":
            current = int(event.get("output_index") or 1)
            total = max(1, int(event.get("output_count") or 1))
            self.progress_bar.setValue(25 + round(current / total * 65))
        elif stage == "completed":
            self.progress_bar.setValue(100)

    def _on_success(self, result: MontageBatchResult) -> None:
        self._last_batch_dir = Path(result.batch_dir)
        self.open_output_button.setEnabled(True)
        self._populate_results(result.outputs)
        self.progress_bar.setValue(100)
        success = sum(item.get("status") == "success" for item in result.outputs)
        failed = len(result.outputs) - success
        narration = getattr(result, "narration", None)
        narration_summary = ""
        if isinstance(narration, dict):
            voice_id = str(narration.get("voice_id") or "系统配音")
            duration = int(narration.get("master_duration_ms") or 0) / 1000
            narration_summary = f"；系统配音：{voice_id}，主音轨 {duration:g} 秒"
        if failed:
            self.status_label.setStyleSheet(f"color: {COLORS['warning']};")
            self.status_label.setText(
                f"生成结束：成功 {success} 条，失败 {failed} 条{narration_summary}；可在结果中查看"
            )
        else:
            self.status_label.setStyleSheet(f"color: {COLORS['success']};")
            self.status_label.setText(f"已生成 {success} 条{narration_summary}，检查无误后再进入发布")

    def _on_error(self, message: str) -> None:
        self.status_label.setStyleSheet(f"color: {COLORS['danger']};")
        self.status_label.setText(f"生成失败：{message}")
        self.progress_bar.setValue(0)

    def _on_finished(self) -> None:
        self.start_button.setEnabled(True)
        self.refresh_button.setEnabled(True)

    def _populate_results(self, outputs: tuple[dict[str, object], ...] | list[dict[str, object]]) -> None:
        self.result_table.setRowCount(len(outputs))
        for row, output in enumerate(outputs):
            success = output.get("status") == "success"
            self.result_table.setItem(
                row,
                0,
                table_item("成功" if success else "失败", COLORS["success"] if success else COLORS["danger"]),
            )
            self.result_table.setItem(row, 1, table_item(output.get("title") or output.get("error") or ""))
            body_item = table_item(output.get("body") or "")
            body_item.setToolTip(str(output.get("body") or ""))
            self.result_table.setItem(row, 2, body_item)
            duration_ms = int(output.get("duration_ms") or 0)
            self.result_table.setItem(row, 3, table_item(f"{duration_ms / 1000:g} 秒" if duration_ms else "—"))
            fingerprint = str(output.get("fingerprint") or "")
            fingerprint_item = table_item(fingerprint[:12] if fingerprint else "—")
            fingerprint_item.setToolTip(fingerprint)
            self.result_table.setItem(row, 4, fingerprint_item)
            output_path = str(output.get("output_path") or "")
            file_item = table_item(Path(output_path).name if output_path else "—")
            file_item.setData(Qt.ItemDataRole.UserRole, output_path)
            file_item.setToolTip(output_path)
            self.result_table.setItem(row, 5, file_item)
            self.result_table.setRowHeight(row, 38)

    def open_result_video(self, row: int, _column: int) -> None:
        item = self.result_table.item(row, 5)
        path = str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""
        if path and Path(path).is_file():
            open_path(path)

    def open_output_directory(self) -> None:
        if self._last_batch_dir and self._last_batch_dir.exists():
            reveal_in_folder(self._last_batch_dir)

    def shutdown(self) -> bool:
        if self.runner.is_running(self.TASK_KEY):
            QMessageBox.information(
                self,
                "自动混剪仍在运行",
                "当前视频还在本地生成。请等待完成后再关闭客户端，避免留下不完整结果。",
            )
            return False
        return True
