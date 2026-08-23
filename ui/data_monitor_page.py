# -*- coding: utf-8 -*-
"""平台数据监测页。"""

from __future__ import annotations

import math
import time

from PyQt6.QtCore import QDate, QDateTime, QSettings, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app_core import (
    account_service,
    platform_data_comment_service,
    platform_data_service,
    platform_data_sync,
)
from app_core.platform_data_collectors import registered_platform_types
from app_core.platform_data_collection_errors import PUBLIC_PLATFORM_DATA_ERROR_TEXT
from app_core.platform_data_comment_ai import OpenAiCompatibleCommentProvider
from app_core.platform_data_comment_secret_store import CommentSecretStore
from app_core.platform_data_comment_settings import (
    CommentAiSettings,
    load_ai_settings,
    save_ai_settings,
)

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
    "unsupported": "平台未提供",
}

SOURCE_TEXT = {
    "direct_session": "会话直连",
    "browser_signed": "官方签名读取",
    "mixed": "混合来源",
}

PROGRESS_TEXT = {
    "direct_session": "正在读取已登录账号数据…",
    "browser_signed": "正在读取平台官方数据…",
    "account_metrics": "正在整理账号趋势…",
    "content_list": "正在读取作品列表…",
    "content_metrics": "正在整理作品指标…",
    "persisting": "正在保存可信指标…",
    "completed": "数据同步完成",
    "partial": "部分数据已保存",
    "failed": "数据同步未完成",
}

COMMENT_LABELS = ("质疑", "认同", "真实经历", "追问", "选题建议", "其他")

COMMENT_ERROR_TEXT = {
    "comment_content_unavailable": "所选作品暂时无法同步评论",
    "comment_login_required": "抖音登录状态已失效",
    "comment_verification_required": "请先在抖音完成人工验证",
    "comment_access_denied": "当前账号暂无评论读取权限",
    "comment_payload_invalid": "评论数据暂未接通，请稍后再试",
    "comment_sync_timeout": "评论同步超时，请重试",
    "comment_sync_cancelled": "评论同步已取消",
    "comment_ai_not_configured": "AI 未配置",
    "comment_ai_timeout": "AI 洞察超时",
    "comment_ai_service_unavailable": "AI 洞察服务暂不可用",
    "comment_ai_response_invalid": "AI 洞察结果不合格",
    "comment_ai_evidence_invalid": "AI 洞察证据不合格",
}

ERROR_TEXT = PUBLIC_PLATFORM_DATA_ERROR_TEXT

_MONITOR_PLATFORM_ORDER = (3, 1, 10, 2, 5, 4)
_DEMO_ACCOUNT_FILE_PATH = "__oneclick_demo_wechat__.json"

_DIAGNOSTIC_ENDPOINT_TEXT = {
    "account_home": "账号主页",
    "account_base": "账号数据",
    "content_list": "作品列表",
    "content_detail": "作品详情",
    "runtime": "页面读回",
}
_DIAGNOSTIC_STAGE_TEXT = {
    "response_capture": "响应采集",
    "response_headers": "响应头",
    "response_body": "响应正文读取",
    "request_binding": "请求绑定",
    "json_decode": "JSON解析",
    "account_parse": "账号数据解析",
    "content_list_parse": "作品列表解析",
    "content_detail_parse": "作品详情解析",
    "visible_readback": "页面读回",
    "browser_start": "浏览器启动",
    "context_create": "会话创建",
    "page_create": "页面创建",
    "navigation": "页面导航",
    "runtime": "运行环境",
}
_DIAGNOSTIC_REASON_TEXT = {
    "duplicate_response": "响应重复",
    "invalid_content_length": "长度头无效",
    "body_unavailable": "正文读取不可用",
    "body_read_failed": "正文读取失败",
    "body_size_invalid": "正文大小无效",
    "request_mismatch": "请求与作品不匹配",
    "invalid_json": "JSON格式无效",
    "payload_shape_invalid": "数据结构不匹配",
    "readback_mismatch": "页面读回不一致",
    "operation_failed": "操作失败",
    "invalid_navigation": "导航目标无效",
    "request_limit_exceeded": "请求数量超过上限",
    "response_limit_exceeded": "响应数量超过上限",
}


def _failure_diagnostic_text(value: object) -> str:
    if type(value) is not dict or set(value) != {"endpoint", "stage", "reason"}:
        return ""
    endpoint = _DIAGNOSTIC_ENDPOINT_TEXT.get(value.get("endpoint"))
    stage = _DIAGNOSTIC_STAGE_TEXT.get(value.get("stage"))
    reason = _DIAGNOSTIC_REASON_TEXT.get(value.get("reason"))
    if not endpoint or not stage or not reason:
        return ""
    return f"接口={endpoint}；阶段={stage}；原因={reason}"


def _is_demo_account(account: dict) -> bool:
    """监测页只接纳可执行真实同步的账号，展示占位账号留在账号管理。"""

    if account.get("filePath") == _DEMO_ACCOUNT_FILE_PATH:
        return True
    for field in ("remark", "profileName", "userName"):
        value = account.get(field)
        if type(value) is not str:
            continue
        text = value.strip()
        if (
            text.startswith("演示账号")
            or text.endswith("（演示）")
            or text.endswith("(演示)")
        ):
            return True
    return False


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


def _comment_error_text(value: object) -> str:
    code = value if type(value) is str else ""
    return COMMENT_ERROR_TEXT.get(code, "评论同步未完成")


def _build_comment_ai_provider():
    """只在后台洞察开始时读取密钥，不把旧密钥交给设置界面。"""

    settings = load_ai_settings(QSettings())
    if settings is None:
        return None
    secret = None
    try:
        secret = CommentSecretStore().read()
        if type(secret) is not str or not secret:
            return None
        return OpenAiCompatibleCommentProvider(settings=settings, secret=secret)
    finally:
        secret = None


class _CommentAiSettingsDialog(QDialog):
    """只编辑非秘密配置和一次性新密钥，从不读回旧密钥。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        settings=None,
        secret_store=None,
        secret_configured: bool | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("AI 设置")
        self.setModal(True)
        self._settings = settings if settings is not None else QSettings()
        self._secret_store = (
            secret_store if secret_store is not None else CommentSecretStore()
        )
        loaded = load_ai_settings(self._settings)
        self._secret_configured = (
            bool(secret_configured)
            if type(secret_configured) is bool
            else loaded is not None
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        form = QFormLayout()
        self.base_url_input = QLineEdit()
        self.base_url_input.setPlaceholderText("https://ai.example.com/v1")
        self.model_input = QLineEdit()
        self.model_input.setPlaceholderText("模型名称")
        self.secret_input = QLineEdit()
        self.secret_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret_input.setPlaceholderText("留空则不替换现有密钥")
        if loaded is not None:
            self.base_url_input.setText(loaded.normalized_base_url)
            self.model_input.setText(loaded.model)
        form.addRow("HTTPS 基础地址", self.base_url_input)
        form.addRow("模型", self.model_input)
        form.addRow("新 API Key", self.secret_input)
        layout.addLayout(form)

        self.secret_status_label = QLabel()
        self.secret_status_label.setProperty("role", "muted")
        layout.addWidget(self.secret_status_label)
        self.feedback_label = QLabel("")
        self.feedback_label.setProperty("role", "muted")
        self.feedback_label.setWordWrap(True)
        layout.addWidget(self.feedback_label)

        actions = QHBoxLayout()
        self.clear_secret_button = button("清除密钥", variant="secondary")
        self.clear_secret_button.clicked.connect(self._clear_secret)
        actions.addWidget(self.clear_secret_button)
        actions.addStretch()
        close_button = button("关闭", variant="secondary")
        close_button.clicked.connect(self.reject)
        actions.addWidget(close_button)
        self.save_button = button("保存设置", variant="primary")
        self.save_button.clicked.connect(self._save)
        actions.addWidget(self.save_button)
        layout.addLayout(actions)
        self._render_secret_status()

    def _render_secret_status(self) -> None:
        state = "已配置" if self._secret_configured else "未配置"
        self.secret_status_label.setText(f"密钥状态：{state}")

    def _save(self) -> None:
        try:
            value = CommentAiSettings(
                self.base_url_input.text(),
                self.model_input.text(),
            )
            new_secret = self.secret_input.text()
            if new_secret:
                self._secret_store.write(new_secret)
                self._secret_configured = True
            save_ai_settings(self._settings, value)
        except Exception:
            self.secret_input.clear()
            self.feedback_label.setText("设置未保存，请检查 HTTPS 地址、模型和密钥")
            self._render_secret_status()
            return
        self.secret_input.clear()
        self.feedback_label.setText("")
        self._render_secret_status()
        self.accept()

    def _clear_secret(self) -> None:
        try:
            self._secret_store.delete()
        except Exception:
            self.feedback_label.setText("密钥未清除，请稍后再试")
            return
        self._secret_configured = False
        self.secret_input.clear()
        self.feedback_label.setText("")
        self._render_secret_status()


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
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
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
            title_item = self._text_cell(item.get("title"))
            content_id = item.get("contentId")
            if (
                type(content_id) is str
                and bool(content_id)
                and content_id == content_id.strip()
            ):
                title_item.setData(Qt.ItemDataRole.UserRole, content_id)
            self.setItem(row_index, 0, title_item)
            self.setItem(row_index, 1, self._timestamp_cell(item.get("publishedAt")))
            self.setItem(row_index, 2, self._text_cell(item.get("contentStatus")))
            metrics = item.get("metrics")
            if type(metrics) is not dict:
                metrics = {}
            for metric_key, column in self._METRIC_COLUMNS:
                self.setItem(row_index, column, self._metric_cell(metrics.get(metric_key)))

    def selected_content(self) -> dict | None:
        rows = self.selectionModel().selectedRows() if self.selectionModel() else []
        if len(rows) != 1:
            return None
        title_item = self.item(rows[0].row(), 0)
        if title_item is None:
            return None
        content_id = title_item.data(Qt.ItemDataRole.UserRole)
        if (
            type(content_id) is not str
            or not content_id
            or content_id != content_id.strip()
        ):
            return None
        return {"contentId": content_id, "title": title_item.text()}


class DataMonitorPage(QWidget):
    request_account_management = pyqtSignal()

    def __init__(self, *, task_runner=None, comment_ai_provider_factory=None) -> None:
        super().__init__()
        self.setObjectName("pageRoot")
        self.runner = task_runner or BackgroundTaskRunner(self)
        self.metric_titles: dict[str, QLabel] = {}
        self.metric_values: dict[str, QLabel] = {}
        self.metric_captions: dict[str, QLabel] = {}
        self._content_limit = 50
        self._content_offset = 0
        self._content_total = 0
        self._content_covered_count = 0
        self._content_availability = "missing"
        self._content_warning_code = ""
        self._shutting_down = False
        self._sync_terminal_by_subject: dict[tuple[int, int], str] = {}
        self._accounts_by_platform: dict[int, tuple[dict, ...]] = {}
        self._last_account_by_platform: dict[int, int] = {}
        self._selected_platform_type: int | None = None
        self._comment_task_generation: dict[str, int] = {}
        self._comment_rows: tuple[dict, ...] = ()
        self._comment_candidates: tuple[dict, ...] = ()
        self._comment_panel_identity: tuple[int, int, str] | None = None
        self._comment_ai_provider_factory = (
            comment_ai_provider_factory
            if callable(comment_ai_provider_factory)
            else _build_comment_ai_provider
        )
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
        self.platform_combo = QComboBox()
        self.platform_combo.setMinimumWidth(132)
        self.platform_combo.currentIndexChanged.connect(self._platform_changed)
        header.addWidget(self.platform_combo)
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
        self.content_metadata_label = QLabel("")
        self.content_metadata_label.setProperty("role", "muted")
        self.content_metadata_label.hide()
        contents_header.addWidget(self.content_metadata_label)
        self.previous_page_button = button("上一页", variant="secondary", compact=True)
        self.previous_page_button.clicked.connect(lambda: self._change_content_page(-1))
        contents_header.addWidget(self.previous_page_button)
        self.next_page_button = button("下一页", variant="secondary", compact=True)
        self.next_page_button.clicked.connect(lambda: self._change_content_page(1))
        contents_header.addWidget(self.next_page_button)
        contents_layout.addLayout(contents_header)
        self.content_table = _ContentTable()
        self.content_table.setMinimumHeight(210)
        self.content_table.itemSelectionChanged.connect(
            self._comment_selection_changed
        )
        contents_layout.addWidget(self.content_table)
        layout.addWidget(contents_panel)

        self.comment_panel = QFrame()
        self.comment_panel.setProperty("panel", True)
        comment_layout = QVBoxLayout(self.comment_panel)
        comment_layout.setContentsMargins(16, 14, 16, 14)
        comment_layout.setSpacing(10)
        comment_header = QHBoxLayout()
        comment_header.addWidget(QLabel("评论洞察"))
        self.comment_selected_title_label = QLabel("请先选择一篇作品")
        self.comment_selected_title_label.setProperty("role", "muted")
        comment_header.addWidget(self.comment_selected_title_label, 1)
        self.comment_last_sync_label = QLabel("最近同步：—")
        self.comment_last_sync_label.setProperty("role", "muted")
        comment_header.addWidget(self.comment_last_sync_label)
        self.comment_ai_settings_button = button("AI 设置", variant="secondary")
        self.comment_ai_settings_button.clicked.connect(
            self._open_comment_ai_settings
        )
        comment_header.addWidget(self.comment_ai_settings_button)
        self.comment_sync_button = button("同步最新评论", variant="primary")
        self.comment_sync_button.clicked.connect(self._start_comment_sync)
        comment_header.addWidget(self.comment_sync_button)
        comment_layout.addLayout(comment_header)

        self.comment_status_label = QLabel("请先选择一篇作品")
        self.comment_status_label.setWordWrap(True)
        comment_layout.addWidget(self.comment_status_label)

        filter_layout = QHBoxLayout()
        filter_layout.addWidget(QLabel("分类筛选"))
        self.comment_filter_combo = QComboBox()
        self.comment_filter_combo.addItems(("全部",) + COMMENT_LABELS)
        self.comment_filter_combo.currentIndexChanged.connect(
            self._apply_comment_filter
        )
        filter_layout.addWidget(self.comment_filter_combo)
        filter_layout.addStretch()
        comment_layout.addLayout(filter_layout)

        self.comment_table = QTableWidget(0, 5)
        self.comment_table.setHorizontalHeaderLabels(
            ("评论正文", "点赞数", "回复数", "评论时间", "分类")
        )
        self.comment_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.comment_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.comment_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.comment_table.verticalHeader().setVisible(False)
        comment_table_header = self.comment_table.horizontalHeader()
        comment_table_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 5):
            comment_table_header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        self.comment_table.setMinimumHeight(170)
        comment_layout.addWidget(self.comment_table)

        comment_layout.addWidget(QLabel("下期选题候选"))
        self.comment_candidate_tree = QTreeWidget()
        self.comment_candidate_tree.setColumnCount(1)
        self.comment_candidate_tree.setHeaderHidden(True)
        self.comment_candidate_tree.setMinimumHeight(120)
        comment_layout.addWidget(self.comment_candidate_tree)
        layout.addWidget(self.comment_panel)

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

    def _current_comment_identity(self) -> tuple[int, int, str] | None:
        subject = self._current_subject_identity()
        selected = self.content_table.selected_content()
        if subject is None or subject[0] != 3 or selected is None:
            return None
        return subject[0], subject[1], selected["contentId"]

    @staticmethod
    def _comment_sync_key(identity: tuple[int, int, str]) -> str:
        platform_type, account_id, content_id = identity
        return f"platform-comment-sync:{platform_type}:{account_id}:{content_id}"

    def _is_task_running(self, key: str) -> bool:
        is_running = getattr(self.runner, "is_running", None)
        if callable(is_running):
            return bool(is_running(key))
        return key in self.runner.active_keys_with_prefixes(("platform-comment-sync",))

    def _clear_comment_projection(self) -> None:
        self._comment_rows = ()
        self._comment_candidates = ()
        self.comment_table.setRowCount(0)
        self.comment_candidate_tree.clear()
        self.comment_last_sync_label.setText("最近同步：—")
        self.comment_filter_combo.blockSignals(True)
        self.comment_filter_combo.setCurrentIndex(0)
        self.comment_filter_combo.blockSignals(False)

    def _update_comment_controls(self) -> None:
        is_douyin = self._current_platform_type() == 3
        identity = self._current_comment_identity()
        running = (
            self._is_task_running(self._comment_sync_key(identity))
            if identity is not None
            else False
        )
        unavailable = self._content_availability in {"unavailable", "missing"}
        self.comment_sync_button.setEnabled(
            is_douyin
            and identity is not None
            and not unavailable
            and not running
            and not self._shutting_down
        )
        self.comment_ai_settings_button.setEnabled(
            is_douyin and not running and not self._shutting_down
        )
        self.comment_filter_combo.setEnabled(
            is_douyin and identity is not None and bool(self._comment_rows)
        )

    def _comment_selection_changed(self) -> None:
        self._render_comment_panel()

    def _render_comment_panel(self) -> None:
        is_douyin = self._current_platform_type() == 3
        self.comment_panel.setVisible(is_douyin)
        if not is_douyin:
            self._comment_panel_identity = None
            self._clear_comment_projection()
            self.comment_selected_title_label.setText("请先选择一篇作品")
            self.comment_status_label.setText("评论洞察首版仅支持抖音")
            self._update_comment_controls()
            return

        identity = self._current_comment_identity()
        if identity != self._comment_panel_identity:
            self._comment_panel_identity = identity
            self._clear_comment_projection()
        if self._content_availability in {"unavailable", "missing"}:
            self.comment_selected_title_label.setText("请先选择一篇作品")
            self.comment_status_label.setText(
                "抖音作品列表尚未取得，暂时无法同步评论"
            )
            self._update_comment_controls()
            return
        selected = self.content_table.selected_content()
        if identity is None or selected is None:
            self.comment_selected_title_label.setText("请先选择一篇作品")
            self.comment_status_label.setText("请先选择一篇作品")
            self._update_comment_controls()
            return

        self.comment_selected_title_label.setText(f"作品：{selected['title']}")
        try:
            payload = platform_data_comment_service.comment_panel_payload(
                identity[1], identity[2], "全部", 100
            )
            self._install_comment_payload(payload)
        except Exception as exc:
            self._clear_comment_projection()
            code = getattr(exc, "error_code", "comment_payload_invalid")
            self.comment_status_label.setText(_comment_error_text(code))
        self._update_comment_controls()

    @staticmethod
    def _safe_comment_row(value: object) -> dict | None:
        if type(value) is not dict or set(value) != {
            "ref",
            "body",
            "likeCount",
            "replyCount",
            "commentedAt",
            "labels",
        }:
            return None
        ref = value.get("ref")
        body = value.get("body")
        likes = value.get("likeCount")
        replies = value.get("replyCount")
        commented_at = value.get("commentedAt")
        labels = value.get("labels")
        if (
            type(ref) is not str
            or len(ref) != 4
            or not ref.startswith("C")
            or not ref[1:].isdigit()
            or type(body) is not str
            or not body
            or type(likes) is not int
            or likes < 0
            or type(replies) is not int
            or replies < 0
            or not _valid_timestamp(commented_at)
            or type(labels) is not list
            or any(type(label) is not str or label not in COMMENT_LABELS for label in labels)
            or len(labels) != len(set(labels))
        ):
            return None
        return {
            "ref": ref,
            "body": body,
            "likeCount": likes,
            "replyCount": replies,
            "commentedAt": commented_at,
            "labels": tuple(labels),
        }

    def _install_comment_payload(self, payload: object) -> None:
        if type(payload) is not dict or set(payload) != {
            "title",
            "lastSyncAt",
            "comments",
            "candidates",
            "aiStatus",
            "aiErrorCode",
        }:
            raise ValueError("invalid comment panel payload")
        raw_rows = payload.get("comments")
        raw_candidates = payload.get("candidates")
        if type(raw_rows) is not list or type(raw_candidates) is not list:
            raise ValueError("invalid comment panel payload")
        rows = []
        by_ref: dict[str, dict] = {}
        for raw in raw_rows:
            row = self._safe_comment_row(raw)
            if row is None or row["ref"] in by_ref:
                raise ValueError("invalid comment panel payload")
            rows.append(row)
            by_ref[row["ref"]] = row

        candidates = []
        for raw_candidate in raw_candidates:
            if type(raw_candidate) is not dict or set(raw_candidate) != {
                "title",
                "reason",
                "evidence",
            }:
                raise ValueError("invalid comment panel payload")
            title = raw_candidate.get("title")
            reason = raw_candidate.get("reason")
            evidence = raw_candidate.get("evidence")
            if (
                type(title) is not str
                or not title
                or title != title.strip()
                or type(reason) is not str
                or not reason
                or reason != reason.strip()
                or type(evidence) is not list
                or not evidence
            ):
                raise ValueError("invalid comment panel payload")
            canonical_evidence = []
            seen_refs = set()
            for raw_evidence in evidence:
                if type(raw_evidence) is not dict:
                    raise ValueError("invalid comment panel payload")
                ref = raw_evidence.get("ref")
                if type(ref) is not str or ref in seen_refs or ref not in by_ref:
                    raise ValueError("invalid comment panel payload")
                seen_refs.add(ref)
                canonical_evidence.append(by_ref[ref])
            candidates.append(
                {
                    "title": title,
                    "reason": reason,
                    "evidence": tuple(canonical_evidence),
                }
            )

        last_sync_at = payload.get("lastSyncAt")
        if last_sync_at != "" and not _valid_timestamp(last_sync_at):
            raise ValueError("invalid comment panel payload")
        ai_status = payload.get("aiStatus")
        ai_error_code = payload.get("aiErrorCode")
        if ai_status not in {"success", "failed", "skipped"} or type(ai_error_code) is not str:
            raise ValueError("invalid comment panel payload")

        self._comment_rows = tuple(rows)
        self._comment_candidates = tuple(candidates)
        self.comment_last_sync_label.setText(
            f"最近同步：{last_sync_at or '—'}"
        )
        if ai_status == "success":
            self.comment_status_label.setText("评论与洞察已从本地读取")
        elif ai_status == "skipped" and ai_error_code == "comment_ai_not_configured":
            self.comment_status_label.setText("评论已同步，AI 未配置")
        elif ai_status == "failed":
            self.comment_status_label.setText(
                f"评论已同步，洞察未生成：{_comment_error_text(ai_error_code)}"
            )
        else:
            raise ValueError("invalid comment panel payload")
        self._apply_comment_filter()
        self._render_comment_candidates()

    def _apply_comment_filter(self) -> None:
        if not hasattr(self, "comment_table"):
            return
        selected_label = self.comment_filter_combo.currentText()
        rows = [
            row
            for row in self._comment_rows
            if selected_label == "全部" or selected_label in row["labels"]
        ]
        self.comment_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = (
                row["body"],
                str(row["likeCount"]),
                str(row["replyCount"]),
                row["commentedAt"],
                "、".join(row["labels"]) or "未分类",
            )
            for column, value in enumerate(values):
                self.comment_table.setItem(row_index, column, QTableWidgetItem(value))

    def _render_comment_candidates(self) -> None:
        self.comment_candidate_tree.clear()
        for candidate in self._comment_candidates:
            item = QTreeWidgetItem(
                [f"{candidate['title']}：{candidate['reason']}"]
            )
            for evidence in candidate["evidence"]:
                item.addChild(QTreeWidgetItem([f"证据：{evidence['body']}"]))
            self.comment_candidate_tree.addTopLevelItem(item)

    def _open_comment_ai_settings(self) -> None:
        if self._shutting_down or self._current_platform_type() != 3:
            return
        _CommentAiSettingsDialog(self).exec()

    def _start_comment_sync(self) -> None:
        if self._shutting_down:
            return
        identity = self._current_comment_identity()
        if identity is None or self._content_availability in {"unavailable", "missing"}:
            self._update_comment_controls()
            return
        key = self._comment_sync_key(identity)
        if self._is_task_running(key):
            self._update_comment_controls()
            return
        generation = self._comment_task_generation.get(key, 0) + 1
        previous_generation = self._comment_task_generation.get(key)
        self._comment_task_generation[key] = generation
        terminal_state = {"value": "pending"}

        def is_current() -> bool:
            return (
                not self._shutting_down
                and self._current_comment_identity() == identity
                and self._comment_task_generation.get(key) == generation
            )

        def started() -> None:
            terminal_state["value"] = "running"
            if is_current():
                self.comment_status_label.setText(
                    platform_data_comment_service.COMMENT_PROGRESS["preparing"]
                )
                self._update_comment_controls()

        def progressed(payload: object) -> None:
            if not is_current() or type(payload) is not dict or set(payload) != {
                "stage",
                "message",
            }:
                return
            stage = payload.get("stage")
            message = payload.get("message")
            expected = (
                platform_data_comment_service.COMMENT_PROGRESS.get(stage)
                if type(stage) is str
                else None
            )
            if expected is None or message != expected:
                return
            self.comment_status_label.setText(expected)

        def completed(result: object) -> None:
            if not is_current():
                return
            if type(result) is not dict or result.get("status") != "success":
                terminal_state["value"] = "failed"
                code = result.get("errorCode") if type(result) is dict else ""
                self.comment_status_label.setText(_comment_error_text(code))
                return
            terminal_state["value"] = "success"
            self._render_comment_panel()

        def failed(_message: str) -> None:
            terminal_state["value"] = "failed"
            if is_current():
                self.comment_status_label.setText("评论同步未完成")

        def finished() -> None:
            if is_current():
                self._update_comment_controls()

        accepted = self.runner.run(
            key,
            with_progress=lambda report: platform_data_comment_service.sync_comments(
                identity[1],
                identity[2],
                report=report,
                ai_provider_factory=self._comment_ai_provider_factory,
            ),
            on_started=started,
            on_progress=progressed,
            on_success=completed,
            on_error=failed,
            on_finished=finished,
        )
        if not accepted:
            if previous_generation is None:
                self._comment_task_generation.pop(key, None)
            else:
                self._comment_task_generation[key] = previous_generation
            self._update_comment_controls()
            return
        self.comment_status_label.setText(
            platform_data_comment_service.COMMENT_PROGRESS["preparing"]
        )
        self._update_comment_controls()

    def refresh(self) -> None:
        current_platform = self._current_platform_type()
        current_account = self._current_account_id()
        if current_platform is not None and current_account is not None:
            self._last_account_by_platform[current_platform] = current_account

        registered = {
            platform_type
            for platform_type in registered_platform_types()
            if type(platform_type) is int and platform_type > 0
        }
        accounts_by_platform: dict[int, list[dict]] = {}
        for account in account_service.list_accounts():
            if type(account) is not dict:
                continue
            if _is_demo_account(account):
                continue
            platform_type = account.get("type")
            account_id = account.get("id")
            if (
                type(platform_type) is not int
                or platform_type not in registered
                or type(account_id) is not int
                or account_id <= 0
            ):
                continue
            accounts_by_platform.setdefault(platform_type, []).append(account)
        platform_order = (
            _MONITOR_PLATFORM_ORDER
            + tuple(
                platform_type
                for platform_type in account_service.PLATFORM_ORDER
                if platform_type not in _MONITOR_PLATFORM_ORDER
            )
        )
        self._accounts_by_platform = {
            platform_type: tuple(accounts_by_platform[platform_type])
            for platform_type in platform_order
            if platform_type in accounts_by_platform
        }
        platform_types = tuple(self._accounts_by_platform)

        preferred_platform = (
            current_platform if current_platform in self._accounts_by_platform else None
        )
        if preferred_platform is None and platform_types:
            preferred_platform = platform_types[0]

        self.platform_combo.blockSignals(True)
        self.platform_combo.clear()
        for platform_type in platform_types:
            self.platform_combo.addItem(
                account_service.PLATFORMS.get(platform_type, f"平台 {platform_type}"),
                platform_type,
            )
        if preferred_platform is not None:
            self.platform_combo.setCurrentIndex(
                self.platform_combo.findData(preferred_platform)
            )
        self.platform_combo.blockSignals(False)
        self._selected_platform_type = self._current_platform_type()
        preferred_account = (
            self._last_account_by_platform.get(self._selected_platform_type)
            if self._selected_platform_type is not None
            else None
        )
        self._refresh_subjects(preferred_account_id=preferred_account)

    def _current_platform_type(self) -> int | None:
        platform_type = self.platform_combo.currentData()
        return platform_type if type(platform_type) is int and platform_type > 0 else None

    def _current_account_id(self) -> int | None:
        account_id = self.account_combo.currentData()
        return account_id if type(account_id) is int and account_id > 0 else None

    def _current_subject_identity(self) -> tuple[int, int] | None:
        platform_type = self._current_platform_type()
        account_id = self._current_account_id()
        if platform_type is None or account_id is None:
            return None
        return platform_type, account_id

    @staticmethod
    def _sync_key(subject_identity: tuple[int, int]) -> str:
        platform_type, account_id = subject_identity
        return f"platform-data-sync:{platform_type}:{account_id}"

    @staticmethod
    def _subject_label(account: dict) -> str:
        account_id = account["id"]
        profile_name = account.get("profileName")
        user_name = account.get("userName")
        profile = profile_name.strip() if type(profile_name) is str else ""
        user = user_name.strip() if type(user_name) is str else ""
        return f"{profile or '未命名主体'}｜{user or f'账号 {account_id}'}"

    def _refresh_subjects(
        self,
        *,
        preferred_account_id: int | None = None,
    ) -> None:
        platform_type = self._current_platform_type()
        accounts = (
            self._accounts_by_platform.get(platform_type, ())
            if platform_type is not None
            else ()
        )
        self.account_combo.blockSignals(True)
        self.account_combo.clear()
        for account in accounts:
            self.account_combo.addItem(self._subject_label(account), account["id"])
        if type(preferred_account_id) is int and preferred_account_id > 0:
            index = self.account_combo.findData(preferred_account_id)
            if index >= 0:
                self.account_combo.setCurrentIndex(index)
        self.account_combo.blockSignals(False)
        self._render_selected_account()

    def _platform_changed(self) -> None:
        previous_platform = self._selected_platform_type
        previous_account = self._current_account_id()
        if previous_platform is not None and previous_account is not None:
            self._last_account_by_platform[previous_platform] = previous_account
        self._selected_platform_type = self._current_platform_type()
        self._clear_account_view("正在读取本地主体数据…")
        preferred_account = (
            self._last_account_by_platform.get(self._selected_platform_type)
            if self._selected_platform_type is not None
            else None
        )
        self._refresh_subjects(preferred_account_id=preferred_account)

    def _render_selected_account(self) -> None:
        platform_type = self._current_platform_type()
        account_id = self._current_account_id()
        if platform_type is not None and account_id is not None:
            self._last_account_by_platform[platform_type] = account_id
        self._clear_account_view("正在读取本地主体数据…")
        self._render_data(include_contents=True)

    def _clear_account_view(self, message: str) -> None:
        for value in self.metric_values.values():
            value.setText("—")
        for caption in self.metric_captions.values():
            caption.setText("暂未取得")
        self.period_label.setText("统计区间：—")
        self.source_label.setText("数据来源：—")
        self.platform_observed_label.setText("平台观察时间：—")
        self.local_synced_label.setText("本地同步时间：—")
        self.status_label.setText(message)
        self.relogin_button.hide()
        self.sync_button.setEnabled(False)
        self.trend_chart.set_series({})
        self.content_table.set_payload({"items": []})
        self._content_offset = 0
        self._content_total = 0
        self._content_covered_count = 0
        self._content_availability = "missing"
        self._content_warning_code = ""
        self.content_metadata_label.hide()
        self.content_metadata_label.setText("")
        self._update_content_paging()
        self._render_comment_panel()

    def _render_data(self, *, include_contents: bool) -> None:
        account_id = self.account_combo.currentData()
        if type(account_id) is not int or account_id <= 0:
            platform_type = self._current_platform_type()
            platform_name = account_service.PLATFORMS.get(platform_type, "当前平台")
            self._clear_account_view(f"请先在账号管理中添加{platform_name}账号")
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
        subject_identity = self._current_subject_identity()
        memory_terminal = self._sync_terminal_by_subject.get(subject_identity)
        if memory_terminal == "running":
            self.status_label.setText("正在同步数据…")
        elif memory_terminal == "failed":
            self.status_label.setText("数据同步未完成")
        elif not isinstance(latest, dict):
            self.status_label.setText("尚未同步")
        elif latest.get("status") == "success":
            self.status_label.setText("账号趋势和作品数据已更新")
        elif latest.get("status") == "partial_success":
            self.status_label.setText("账号趋势已更新，作品数据未取得")
        else:
            code = str(latest.get("errorCode") or "")
            self.status_label.setText(
                f"同步失败：{self._error_text(code, self._current_platform_type())}"
            )
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
            if (
                isinstance(latest, dict)
                and latest.get("status") == "partial_success"
                and latest.get("errorCode") == "content_list_truncated"
                and self._content_availability == "partial"
                and self._content_warning_code == "content_list_truncated"
                and type(self._content_covered_count) is int
                and type(self._content_total) is int
                and 0 < self._content_covered_count <= self._content_total
            ):
                self.status_label.setText(
                    f"账号趋势已更新，已取得最近 {self._content_covered_count} 条作品"
                )
        key = self._sync_key(subject_identity) if subject_identity is not None else ""
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
        availability = safe_payload.get("availability")
        self._content_availability = (
            availability
            if availability in {"available", "partial", "unavailable", "missing"}
            else "missing"
        )
        warning_code = safe_payload.get("warningCode")
        self._content_warning_code = (
            warning_code if type(warning_code) is str else ""
        )
        total = safe_payload.get("total")
        self._content_total = total if type(total) is int and total >= 0 else 0
        covered_count = safe_payload.get("coveredCount")
        self._content_covered_count = (
            covered_count
            if type(covered_count) is int and 0 <= covered_count <= self._content_total
            else 0
        )
        self.content_table.set_payload(safe_payload)
        platform_type = self.platform_combo.currentData()
        metadata_missing = (
            platform_type == 1
            and any(
                type(item) is dict
                and (
                    not isinstance(item.get("title"), str)
                    or not item["title"].strip()
                    or not isinstance(item.get("publishedAt"), str)
                    or not item["publishedAt"].strip()
                )
                for item in safe_payload.get("items", ())
                if type(safe_payload.get("items")) is list
            )
        )
        self.content_metadata_label.setText(
            "平台未提供标题与发布时间" if metadata_missing else ""
        )
        self.content_metadata_label.setVisible(metadata_missing)
        self._update_content_paging()
        self._render_comment_panel()

    @staticmethod
    def _error_text(error_code: str, platform_type: int | None) -> str:
        if error_code == "login_required":
            platform_name = account_service.PLATFORMS.get(platform_type, "当前平台")
            return f"需要重新登录{platform_name}"
        if error_code == "verification_required":
            platform_name = account_service.PLATFORMS.get(platform_type, "当前平台")
            return f"需要完成{platform_name}验证"
        return ERROR_TEXT.get(error_code, "数据同步未完成")

    def _update_content_paging(self) -> None:
        first = self._content_offset + 1 if self._content_total else 0
        last = min(self._content_offset + self._content_limit, self._content_total)
        if self._content_availability in {"unavailable", "missing"}:
            if self._content_total:
                paging_text = f"本次未取得，显示历史 {self._content_total} 条"
            else:
                paging_text = "作品数据暂未取得"
        elif self._content_availability == "partial":
            covered_last = min(
                self._content_offset + self._content_covered_count,
                self._content_total,
            )
            paging_text = (
                f"仅取得最近 {self._content_covered_count} 条 · {first}-{covered_last}"
            )
        elif self._content_total:
            paging_text = f"已取得 {self._content_total} 条 · {first}-{last}"
        else:
            paging_text = "已取得 0 条"
        self.content_page_label.setText(paging_text)
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
        subject_identity = self._current_subject_identity()
        if subject_identity is None:
            return
        platform_type, account_id = subject_identity
        key = self._sync_key(subject_identity)
        terminal_state = {"value": "pending"}

        def is_current_subject() -> bool:
            return self._current_subject_identity() == subject_identity

        def started() -> None:
            terminal_state["value"] = "running"
            self._sync_terminal_by_subject[subject_identity] = "running"
            if not self._shutting_down and is_current_subject():
                self.sync_button.setEnabled(False)
                self.relogin_button.hide()
                self.status_label.setText("正在同步数据…")

        def progressed(payload: object) -> None:
            if (
                self._shutting_down
                or not is_current_subject()
                or not isinstance(payload, dict)
            ):
                return
            stage = payload.get("stage")
            text = PROGRESS_TEXT.get(stage) if type(stage) is str else None
            if text:
                self.status_label.setText(text)

        def completed(_result: object) -> None:
            failed_result = type(_result) is dict and _result.get("status") == "failed"
            terminal_state["value"] = "failed" if failed_result else "success"
            if failed_result:
                self._sync_terminal_by_subject[subject_identity] = "failed"
            else:
                self._sync_terminal_by_subject.pop(subject_identity, None)
            if self._shutting_down or not is_current_subject():
                return
            if failed_result:
                error_code = _result.get("errorCode")
                self.relogin_button.setVisible(error_code == "login_required")
                base = self._error_text(
                    error_code if type(error_code) is str else "",
                    platform_type,
                )
                diagnostics = _result.get("diagnostics")
                failure = (
                    diagnostics.get("failure") if type(diagnostics) is dict else None
                )
                detail = _failure_diagnostic_text(failure)
                self.status_label.setText(
                    f"同步失败：{base}" + (f"（{detail}）" if detail else "")
                )
                return
            self.refresh()

        def failed(_message: str) -> None:
            terminal_state["value"] = "failed"
            self._sync_terminal_by_subject[subject_identity] = "failed"
            if not self._shutting_down and is_current_subject():
                self.relogin_button.hide()
                self.status_label.setText("数据同步未完成")

        def finished() -> None:
            if not self._shutting_down and is_current_subject():
                self.sync_button.setEnabled(
                    terminal_state["value"] in {"success", "failed"}
                )

        self.runner.run(
            key,
            with_progress=lambda report: platform_data_sync.sync_account_data(
                account_id,
                report=report,
            ),
            on_started=started,
            on_progress=progressed,
            on_success=completed,
            on_error=failed,
            on_finished=finished,
        )

    def shutdown(self) -> bool:
        self._shutting_down = True
        deadline = time.monotonic() + 5.0
        keys = self.runner.active_keys_with_prefixes(
            ("platform-data-sync", "platform-comment-sync")
        )
        for key in keys:
            if self.runner.cancel_pending(key):
                continue
            remaining = max(0.0, deadline - time.monotonic())
            if not self.runner.wait_for_finished(key, remaining):
                return False
        return True
