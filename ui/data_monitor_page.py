# -*- coding: utf-8 -*-
"""平台数据监测页。"""

from __future__ import annotations

import math
import time

from PyQt6.QtCore import QDate, QDateTime, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app_core import account_service, platform_data_service, platform_data_sync

from .background_task import BackgroundTaskRunner
from .common import button


METRICS = (
    ("views", "账号区间新增播放"),
    ("likes", "账号区间新增点赞"),
    ("comments", "账号区间新增评论"),
    ("shares", "账号区间新增分享"),
    ("followers_total", "账号区间末粉丝总数"),
    ("followers_net", "账号区间净增粉"),
    ("profile_visits", "账号区间主页访问"),
)

AVAILABILITY_TEXT = {
    "complete": "完整数据",
    "partial": "部分日期",
    "missing": "暂未取得",
}

SOURCE_TEXT = {
    "direct_session": "会话直连",
    "browser_signed": "官方签名读取",
}

PROGRESS_TEXT = {
    "direct_session": "正在读取已登录账号数据…",
    "browser_signed": "正在读取抖音官方签名数据…",
    "account_metrics": "正在整理账号趋势…",
    "content_list": "正在读取作品列表…",
    "content_metrics": "正在整理作品指标…",
    "persisting": "正在保存可信指标…",
    "completed": "数据同步完成",
    "partial": "部分数据已保存",
    "failed": "数据同步未完成",
}

ERROR_TEXT = {
    "login_required": "需要重新登录",
    "verification_required": "需要完成平台验证",
    "metric_payload_empty": "平台暂未返回可用数据",
    "metric_payload_invalid": "平台数据暂时无法识别",
    "sync_persist_failed": "本地数据保存失败",
}


def _finite_number(value: object) -> int | float | None:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        return None
    return value


def _valid_day(value: object) -> bool:
    if type(value) is not str:
        return False
    day = QDate.fromString(value, Qt.DateFormat.ISODate)
    return day.isValid() and day.toString(Qt.DateFormat.ISODate) == value


def _valid_timestamp(value: object) -> bool:
    if type(value) is not str:
        return False
    return QDateTime.fromString(value, Qt.DateFormat.ISODate).isValid()


class _TrendChart(QWidget):
    """用平台自然日绘制稀疏趋势，缺失日不补零。"""

    _COLORS = {
        "views": QColor("#0B766E"),
        "likes": QColor("#175CD3"),
        "comments": QColor("#B54708"),
        "shares": QColor("#A63A79"),
        "followers_net": QColor("#067647"),
        "profile_visits": QColor("#475467"),
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._series: dict[str, tuple[tuple[str, int | float], ...]] = {}
        self._visible_keys: tuple[str, ...] = ("views",)
        self.setMinimumHeight(170)

    def set_series(
        self,
        series: object,
    ) -> None:
        cleaned: dict[str, tuple[tuple[str, int | float], ...]] = {}
        if type(series) is dict:
            for key, points in series.items():
                if type(key) is not str or type(points) not in (list, tuple):
                    continue
                valid_points: list[tuple[str, int | float]] = []
                for point in points:
                    if type(point) not in (list, tuple) or len(point) != 2:
                        continue
                    day_text, value = point
                    number = _finite_number(value)
                    if _valid_day(day_text) and number is not None:
                        valid_points.append((day_text, number))
                cleaned[key] = tuple(sorted(valid_points))
        self._series = cleaned
        self.update()

    def set_visible_keys(self, keys: tuple[str, ...]) -> None:
        self._visible_keys = tuple(keys)
        self.update()

    def series_for_test(self, key: str) -> tuple[tuple[str, int | float], ...]:
        return self._series.get(key, ())

    @staticmethod
    def _dated_points(
        points: tuple[tuple[str, int | float], ...],
    ) -> list[tuple[int, int | float]]:
        dated: list[tuple[int, int | float]] = []
        for day_text, value in points:
            number = _finite_number(value)
            if not _valid_day(day_text) or number is None:
                continue
            day = QDate.fromString(day_text, Qt.DateFormat.ISODate)
            dated.append((day.toJulianDay(), number))
        return sorted(dated)

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = self.rect().adjusted(12, 12, -12, -12)
        painter.fillRect(plot, QColor("#F8FAFB"))
        visible = {
            key: self._dated_points(self._series.get(key, ()))
            for key in self._visible_keys
        }
        points = [point for series in visible.values() for point in series]
        if not points:
            painter.setPen(QColor("#667085"))
            painter.drawText(plot, Qt.AlignmentFlag.AlignCenter, "暂未取得趋势数据")
            return
        first_day = min(day for day, _value in points)
        last_day = max(day for day, _value in points)
        minimum = min(float(value) for _day, value in points)
        maximum = max(float(value) for _day, value in points)
        day_span = max(1, last_day - first_day)
        value_span = max(1.0, maximum - minimum)

        for key, series in visible.items():
            if not series:
                continue
            painter.setPen(QPen(self._COLORS.get(key, QColor("#0B766E")), 2.0))
            path = QPainterPath()
            previous_day: int | None = None
            for day, value in series:
                x = plot.left() + ((day - first_day) / day_span) * plot.width()
                y = plot.bottom() - ((float(value) - minimum) / value_span) * plot.height()
                if previous_day is None or day - previous_day > 1:
                    if not path.isEmpty():
                        painter.drawPath(path)
                    path = QPainterPath()
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)
                painter.drawEllipse(int(x) - 2, int(y) - 2, 4, 4)
                previous_day = day
            if not path.isEmpty():
                painter.drawPath(path)


class _ContentTable(QTableWidget):
    _METRIC_COLUMNS = (
        ("views", 3),
        ("likes", 4),
        ("comments", 5),
        ("shares", 6),
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(0, 7, parent)
        self.setObjectName("dataTable")
        self.setHorizontalHeaderLabels(
            ("作品", "发布时间", "状态", "累计播放", "累计点赞", "累计评论", "累计分享")
        )
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)
        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 7):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)

    @staticmethod
    def _text_cell(value: object) -> QTableWidgetItem:
        text = value.strip() if type(value) is str else ""
        return QTableWidgetItem(text or "—")

    @staticmethod
    def _timestamp_cell(value: object) -> QTableWidgetItem:
        return QTableWidgetItem(value if _valid_timestamp(value) else "—")

    @staticmethod
    def _metric_cell(value: object) -> QTableWidgetItem:
        number = _finite_number(value)
        return QTableWidgetItem("—" if number is None else f"{number:,}")

    def set_payload(self, payload: dict) -> None:
        items = payload.get("items") if type(payload) is dict else None
        rows = items if type(items) is list else []
        rows = sorted(
            (item for item in rows if type(item) is dict),
            key=lambda item: str(item.get("publishedAt") or ""),
            reverse=True,
        )
        self.setRowCount(len(rows))
        for row_index, item in enumerate(rows):
            self.setItem(row_index, 0, self._text_cell(item.get("title")))
            self.setItem(row_index, 1, self._timestamp_cell(item.get("publishedAt")))
            self.setItem(row_index, 2, self._text_cell(item.get("contentStatus")))
            metrics = item.get("metrics")
            if type(metrics) is not dict:
                metrics = {}
            for metric_key, column in self._METRIC_COLUMNS:
                self.setItem(row_index, column, self._metric_cell(metrics.get(metric_key)))


class DataMonitorPage(QWidget):
    request_account_management = pyqtSignal()

    def __init__(self, *, task_runner=None) -> None:
        super().__init__()
        self.setObjectName("pageRoot")
        self.runner = task_runner or BackgroundTaskRunner(self)
        self.metric_titles: dict[str, QLabel] = {}
        self.metric_values: dict[str, QLabel] = {}
        self.metric_captions: dict[str, QLabel] = {}
        self._content_limit = 50
        self._content_offset = 0
        self._content_total = 0
        self._shutting_down = False
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(16)

        header = QHBoxLayout()
        title = QLabel("数据监测")
        title.setObjectName("pageTitle")
        header.addWidget(title)
        header.addStretch()
        self.account_combo = QComboBox()
        self.account_combo.setMinimumWidth(240)
        self.account_combo.currentIndexChanged.connect(self._render_selected_account)
        header.addWidget(self.account_combo)
        self.range_combo = QComboBox()
        self.range_combo.setMinimumWidth(112)
        self.range_combo.addItem("昨日", 1)
        self.range_combo.addItem("近 7 天", 7)
        self.range_combo.addItem("近 30 天", 30)
        self.range_combo.setCurrentIndex(self.range_combo.findData(7))
        self.range_combo.currentIndexChanged.connect(self._render_range)
        header.addWidget(self.range_combo)
        self.sync_button = button("同步数据", variant="primary")
        self.sync_button.clicked.connect(self._start_sync)
        header.addWidget(self.sync_button)
        layout.addLayout(header)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        for column in range(4):
            grid.setColumnStretch(column, 1)
        for index, (key, label_text) in enumerate(METRICS):
            panel = QFrame()
            panel.setProperty("panel", True)
            card = QVBoxLayout(panel)
            card.setContentsMargins(16, 14, 16, 14)
            name = QLabel(label_text)
            name.setProperty("role", "muted")
            value = QLabel("—")
            value.setProperty("role", "metric")
            caption = QLabel("暂未取得")
            caption.setProperty("role", "muted")
            self.metric_titles[key] = name
            self.metric_values[key] = value
            self.metric_captions[key] = caption
            card.addWidget(name)
            card.addWidget(value)
            card.addWidget(caption)
            grid.addWidget(panel, index // 4, index % 4)
        layout.addLayout(grid)

        freshness_panel = QFrame()
        freshness_panel.setProperty("panel", True)
        freshness_layout = QGridLayout(freshness_panel)
        freshness_layout.setContentsMargins(16, 12, 16, 12)
        self.period_label = QLabel("统计区间：—")
        self.source_label = QLabel("数据来源：—")
        self.platform_observed_label = QLabel("平台观察时间：—")
        self.local_synced_label = QLabel("本地同步时间：—")
        for label in (
            self.period_label,
            self.source_label,
            self.platform_observed_label,
            self.local_synced_label,
        ):
            label.setProperty("role", "muted")
        freshness_layout.addWidget(self.period_label, 0, 0)
        freshness_layout.addWidget(self.source_label, 0, 1)
        freshness_layout.addWidget(self.platform_observed_label, 1, 0)
        freshness_layout.addWidget(self.local_synced_label, 1, 1)
        layout.addWidget(freshness_panel)

        trend_panel = QFrame()
        trend_panel.setProperty("panel", True)
        trend_layout = QVBoxLayout(trend_panel)
        trend_layout.setContentsMargins(16, 14, 16, 14)
        trend_header = QHBoxLayout()
        trend_header.addWidget(QLabel("账号日趋势"))
        trend_header.addStretch()
        self.trend_combo = QComboBox()
        self.trend_combo.addItem("播放", ("views",))
        self.trend_combo.addItem("互动", ("likes", "comments", "shares"))
        self.trend_combo.addItem("净增粉", ("followers_net",))
        self.trend_combo.currentIndexChanged.connect(self._select_trend)
        trend_header.addWidget(self.trend_combo)
        trend_layout.addLayout(trend_header)
        self.trend_chart = _TrendChart()
        trend_layout.addWidget(self.trend_chart)
        layout.addWidget(trend_panel)

        contents_panel = QFrame()
        contents_panel.setProperty("panel", True)
        contents_layout = QVBoxLayout(contents_panel)
        contents_layout.setContentsMargins(16, 14, 16, 14)
        contents_header = QHBoxLayout()
        contents_header.addWidget(QLabel("作品数据"))
        contents_header.addStretch()
        self.content_page_label = QLabel("共 0 条")
        self.content_page_label.setProperty("role", "muted")
        contents_header.addWidget(self.content_page_label)
        self.previous_page_button = button("上一页", variant="secondary", compact=True)
        self.previous_page_button.clicked.connect(lambda: self._change_content_page(-1))
        contents_header.addWidget(self.previous_page_button)
        self.next_page_button = button("下一页", variant="secondary", compact=True)
        self.next_page_button.clicked.connect(lambda: self._change_content_page(1))
        contents_header.addWidget(self.next_page_button)
        contents_layout.addLayout(contents_header)
        self.content_table = _ContentTable()
        self.content_table.setMinimumHeight(210)
        contents_layout.addWidget(self.content_table)
        layout.addWidget(contents_panel)

        status_panel = QFrame()
        status_panel.setProperty("panel", True)
        status_layout = QHBoxLayout(status_panel)
        status_layout.setContentsMargins(16, 14, 16, 14)
        self.status_label = QLabel("尚未同步")
        self.status_label.setWordWrap(True)
        status_layout.addWidget(self.status_label, 1)
        self.relogin_button = button("重新登录", variant="secondary")
        self.relogin_button.clicked.connect(self.request_account_management.emit)
        self.relogin_button.hide()
        status_layout.addWidget(self.relogin_button)
        layout.addWidget(status_panel)
        layout.addStretch(1)

    def refresh(self) -> None:
        selected = self.account_combo.currentData()
        accounts = [
            account
            for account in account_service.list_accounts()
            if type(account.get("type")) is int and account["type"] == 3
        ]
        self.account_combo.blockSignals(True)
        self.account_combo.clear()
        for account in accounts:
            label = account.get("profileName") or account.get("userName") or f"抖音账号 {account['id']}"
            self.account_combo.addItem(str(label), account["id"])
        if selected is not None:
            index = self.account_combo.findData(selected)
            if index >= 0:
                self.account_combo.setCurrentIndex(index)
        self.account_combo.blockSignals(False)
        self._render_selected_account()

    def _render_selected_account(self) -> None:
        self._render_data(include_contents=True)

    def _render_data(self, *, include_contents: bool) -> None:
        account_id = self.account_combo.currentData()
        if type(account_id) is not int or account_id <= 0:
            for value in self.metric_values.values():
                value.setText("—")
            for caption in self.metric_captions.values():
                caption.setText("暂未取得")
            self.period_label.setText("统计区间：—")
            self.source_label.setText("数据来源：—")
            self.platform_observed_label.setText("平台观察时间：—")
            self.local_synced_label.setText("本地同步时间：—")
            self.status_label.setText("请先在账号管理中添加抖音账号")
            self.relogin_button.hide()
            self.sync_button.setEnabled(False)
            self.trend_chart.set_series({})
            self.content_table.set_payload({"items": []})
            self._content_offset = 0
            self._content_total = 0
            self._update_content_paging()
            return
        days = self.range_combo.currentData()
        if type(days) is not int:
            days = 7
        summary = platform_data_service.account_period_summary(account_id, days)
        metrics = summary.get("metrics") if isinstance(summary, dict) else {}
        if not isinstance(metrics, dict):
            metrics = {}
        for key, value_label in self.metric_values.items():
            metric = metrics.get(key)
            if not isinstance(metric, dict):
                metric = {}
            value = _finite_number(metric.get("value"))
            value_label.setText("—" if value is None else f"{value:,}")
            availability = metric.get("availability")
            if type(availability) is not str:
                availability = None
            self.metric_captions[key].setText(
                AVAILABILITY_TEXT.get(availability, "暂未取得")
            )
        latest = summary.get("latestRun") if isinstance(summary, dict) else None
        period_start = summary.get("periodStart") if isinstance(summary, dict) else None
        period_end = summary.get("periodEnd") if isinstance(summary, dict) else None
        if _valid_day(period_start) and _valid_day(period_end):
            self.period_label.setText(f"统计区间：{period_start} 至 {period_end}")
        else:
            self.period_label.setText("统计区间：—")
        source_mode = (
            summary.get("trustedSourceMode") if isinstance(summary, dict) else None
        )
        if type(source_mode) is not str:
            source_mode = None
        self.source_label.setText(
            f"数据来源：{SOURCE_TEXT.get(source_mode, '—')}"
        )
        platform_observed_at = (
            summary.get("platformObservedAt") if isinstance(summary, dict) else None
        )
        local_synced_at = (
            summary.get("localSyncedAt") if isinstance(summary, dict) else None
        )
        self.platform_observed_label.setText(
            f"平台观察时间：{platform_observed_at if _valid_timestamp(platform_observed_at) else '—'}"
        )
        self.local_synced_label.setText(
            f"本地同步时间：{local_synced_at if _valid_timestamp(local_synced_at) else '—'}"
        )
        self.relogin_button.hide()
        if not isinstance(latest, dict):
            self.status_label.setText("尚未同步")
        elif latest.get("status") == "success":
            self.status_label.setText("账号趋势和作品数据已更新")
        elif latest.get("status") == "partial_success":
            self.status_label.setText("账号趋势已更新，作品数据未取得")
        else:
            code = str(latest.get("errorCode") or "")
            self.status_label.setText(f"同步失败：{ERROR_TEXT.get(code, '数据同步未完成')}")
            if code == "login_required":
                self.relogin_button.show()
        trends = platform_data_service.account_daily_trends(account_id, days)
        self._set_trends(trends)
        if include_contents:
            self._content_offset = 0
            contents = platform_data_service.account_contents(
                account_id,
                limit=self._content_limit,
                offset=self._content_offset,
            )
            self._set_contents(contents)
        key = f"platform-data-sync:{account_id}"
        is_running = getattr(self.runner, "is_running", None)
        if callable(is_running):
            active = bool(is_running(key))
        else:
            active = key in self.runner.active_keys_with_prefixes(("platform-data-sync",))
        self.sync_button.setEnabled(not self._shutting_down and not active)

    def _render_range(self) -> None:
        account_id = self.account_combo.currentData()
        days = self.range_combo.currentData()
        if type(account_id) is not int or account_id <= 0 or type(days) is not int:
            return
        self._render_data(include_contents=False)

    def _select_trend(self) -> None:
        keys = self.trend_combo.currentData()
        if not isinstance(keys, tuple):
            keys = ("views",)
        self.trend_chart.set_visible_keys(keys)

    def _set_trends(self, payload: object) -> None:
        items = payload.get("items") if type(payload) is dict else None
        rows = items if type(items) is list else []
        series: dict[str, list[tuple[str, int | float]]] = {}
        for item in rows:
            if type(item) is not dict or not _valid_day(item.get("date")):
                continue
            metrics = item.get("metrics")
            if type(metrics) is not dict:
                continue
            for key, value in metrics.items():
                number = _finite_number(value)
                if type(key) is str and number is not None:
                    series.setdefault(key, []).append((item["date"], number))
        self.trend_chart.set_series(
            {key: tuple(points) for key, points in series.items()}
        )

    def _set_contents(self, payload: object) -> None:
        safe_payload = payload if type(payload) is dict else {}
        total = safe_payload.get("total")
        self._content_total = total if type(total) is int and total >= 0 else 0
        self.content_table.set_payload(safe_payload)
        self._update_content_paging()

    def _update_content_paging(self) -> None:
        first = self._content_offset + 1 if self._content_total else 0
        last = min(self._content_offset + self._content_limit, self._content_total)
        self.content_page_label.setText(
            f"共 {self._content_total} 条 · {first}-{last}"
        )
        self.previous_page_button.setEnabled(self._content_offset > 0)
        self.next_page_button.setEnabled(
            self._content_offset + self._content_limit < self._content_total
        )

    def _change_content_page(self, direction: int) -> None:
        account_id = self.account_combo.currentData()
        if type(account_id) is not int or account_id <= 0:
            return
        requested_offset = self._content_offset + direction * self._content_limit
        if requested_offset < 0 or requested_offset >= self._content_total:
            return
        self._content_offset = requested_offset
        payload = platform_data_service.account_contents(
            account_id,
            limit=self._content_limit,
            offset=requested_offset,
        )
        self._set_contents(payload)

    def _start_sync(self) -> None:
        if self._shutting_down:
            return
        account_id = self.account_combo.currentData()
        if type(account_id) is not int or account_id <= 0:
            return
        key = f"platform-data-sync:{account_id}"
        terminal_state = {"value": "pending"}

        def is_current_account() -> bool:
            return self.account_combo.currentData() == account_id

        def started() -> None:
            terminal_state["value"] = "running"
            if not self._shutting_down and is_current_account():
                self.sync_button.setEnabled(False)
                self.status_label.setText("正在同步数据…")

        def progressed(payload: object) -> None:
            if (
                self._shutting_down
                or not is_current_account()
                or not isinstance(payload, dict)
            ):
                return
            stage = payload.get("stage")
            text = PROGRESS_TEXT.get(stage) if type(stage) is str else None
            if text:
                self.status_label.setText(text)

        def completed(_result: object) -> None:
            terminal_state["value"] = "success"
            if not self._shutting_down and is_current_account():
                self.refresh()

        def failed(_message: str) -> None:
            terminal_state["value"] = "failed"
            if not self._shutting_down and is_current_account():
                self.status_label.setText("数据同步未完成")

        def finished() -> None:
            if not self._shutting_down and is_current_account():
                self.sync_button.setEnabled(
                    terminal_state["value"] in {"success", "failed"}
                )

        self.runner.run(
            key,
            with_progress=lambda report: platform_data_sync.sync_account_data(account_id, report=report),
            on_started=started,
            on_progress=progressed,
            on_success=completed,
            on_error=failed,
            on_finished=finished,
        )

    def shutdown(self) -> bool:
        self._shutting_down = True
        deadline = time.monotonic() + 5.0
        keys = self.runner.active_keys_with_prefixes(("platform-data-sync",))
        for key in keys:
            if self.runner.cancel_pending(key):
                continue
            remaining = max(0.0, deadline - time.monotonic())
            if not self.runner.wait_for_finished(key, remaining):
                return False
        return True
