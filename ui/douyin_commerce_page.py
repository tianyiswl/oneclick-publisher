# -*- coding: utf-8 -*-
"""抖音带货的分步桌面工作流。

抖音的音乐与位置候选由三个隔离采集器读取；采集页只保存公开候选和用户选择，
不把采集器会话当作正式发布会话，也不即时写入音乐、地点或作品声明。最终执行器
会为每条视频重新进入正式页核验并应用；没有最终确认时不会点击发表。
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from PyQt6.QtCore import QDate, QPoint, QSize, QTime, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import (
    QButtonGroup,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QMenu,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from app_core import (
    account_service,
    douyin_commerce_batch_draft_service,
    douyin_commerce_batch_service,
    douyin_commerce_collectors,
    douyin_commerce_draft_service,
    douyin_commerce_service,
    douyin_commerce_session,
    douyin_favorite_music_cache,
    media_service,
    task_service,
)
from app_core.douyin_commerce_batch_executor import (
    BatchProgressEvent,
    DouyinCommerceBatchExecutor,
)
from app_core.douyin_location_preset_service import (
    list_location_presets,
    save_location_preset,
)
from app_core.douyin_commerce_location_commission import (
    DEFAULT_COMMISSION_FILTER,
    filter_location_candidates,
    normalize_commission_filter,
    normalize_observed_commission_type,
)
from app_core.douyin_verification import verification_broker
from app_core.paths import AVATAR_DIR

from .background_task import BackgroundTaskRunner
from .common import button
from .douyin_verification_dialog import DouyinVerificationDialog
from .runtime_log import ExecutionLogPanel, runtime_log_bus
from .topic_tag_editor import FlowLayout, history_tag_box_height


_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_LOGGER = logging.getLogger(__name__)
_ACCOUNT_AVATAR_SIZE = 54
_VIDEO_PICKER_NAME_LIMIT = 32
_VIDEO_PICKER_MAX_WIDTH = 440
_VIDEO_PICKER_MAX_HEIGHT = 480
_VIDEO_PICKER_ROW_HEIGHT = 68
_BATCH_RUN_KEY = "douyin_commerce_batch_run"
_BATCH_SHARED_LOCATION_SEARCH_KEY = "__shared_location_search__"

COLLECTOR_ERROR_COPY = {
    "collector_start_failed": "采集器启动失败",
    "login_required": "账号需要重新登录",
    "scope_not_confirmed": "地点范围未确认",
    "candidate_panel_missing": "未找到候选面板",
    "candidate_ambiguous": "候选目标不唯一",
    "candidate_empty": "没有读取到候选",
    "rate_limited_or_degraded": "平台可能频控或服务降级",
    "stale_result_discarded": "旧批次结果已丢弃",
    "cleanup_incomplete": "采集器关闭不完整",
    "publish_apply_mismatch": "正式页设置回读不一致",
    "collector_unknown": "采集器发生未知错误",
}


class _ImeAwarePlainTextEdit(QPlainTextEdit):
    """让多行文案框在中文输入法预编辑时也隐藏占位提示。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._configured_placeholder = ""
        self._ime_preedit_active = False
        self.textChanged.connect(self._sync_placeholder_visibility)

    def setPlaceholderText(self, text: str) -> None:
        self._configured_placeholder = str(text)
        self._sync_placeholder_visibility()

    def inputMethodEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        # QPlainTextEdit 的文档在拼音候选阶段仍为空，默认占位文字会与
        # 预编辑文本重叠。预编辑开始就先隐藏；取消且文档仍为空时再恢复。
        self._ime_preedit_active = bool(event.preeditString())
        self._sync_placeholder_visibility()
        super().inputMethodEvent(event)
        self._ime_preedit_active = bool(event.preeditString())
        self._sync_placeholder_visibility()

    def _sync_placeholder_visibility(self) -> None:
        visible_text = ""
        if not self._ime_preedit_active and not self.toPlainText():
            visible_text = self._configured_placeholder
        if super().placeholderText() != visible_text:
            super().setPlaceholderText(visible_text)


def _account_avatar_path(account: dict) -> Path | None:
    """兼容账号记录中的头像绝对路径与本机头像目录。"""

    value = str(account.get("avatarPath") or "").strip()
    if not value:
        return None
    stored_path = Path(value)
    candidates = (stored_path, AVATAR_DIR / stored_path.name)
    return next((path for path in candidates if path.is_file()), None)


def _account_avatar_pixmap(account: dict, size: int) -> QPixmap:
    """生成本机账号头像；缺失时使用账号名首字占位，不读取平台页面。"""

    canvas = QPixmap(size, size)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    clip_path = QPainterPath()
    clip_path.addEllipse(1, 1, size - 2, size - 2)
    painter.setClipPath(clip_path)
    avatar_path = _account_avatar_path(account)
    avatar = QPixmap(str(avatar_path)) if avatar_path else QPixmap()
    if not avatar.isNull():
        scaled = avatar.scaled(
            size,
            size,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        left = max(0, (scaled.width() - size) // 2)
        top = max(0, (scaled.height() - size) // 2)
        painter.drawPixmap(0, 0, scaled.copy(left, top, size, size))
    else:
        painter.fillPath(clip_path, QColor("#2B668B"))
        display_name = _normalized(
            account.get("userName") or account.get("profileName") or "账"
        )
        painter.setPen(Qt.GlobalColor.white)
        font = painter.font()
        font.setBold(True)
        font.setPixelSize(max(12, size // 2))
        painter.setFont(font)
        painter.drawText(
            0,
            0,
            size,
            size,
            Qt.AlignmentFlag.AlignCenter,
            display_name[:1],
        )

    painter.setClipping(False)
    painter.setPen(QPen(QColor("#D0D5DD"), 1))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(1, 1, size - 2, size - 2)
    painter.end()
    return canvas


def _account_identity(account: dict) -> tuple[str, str]:
    """返回账号名与主体；账号名优先使用平台实际名称。"""

    subject = _normalized(account.get("profileName"))
    account_name = _normalized(account.get("userName")) or subject
    return account_name, subject


def _normalized(value: object) -> str:
    return " ".join(str(value or "").replace("\u200b", " ").split())


def _future_schedule_text(date: QDate, time: QTime) -> str:
    """把控件值按北京时间格式化，并拒绝当前或过去的时间。"""

    target = datetime(
        date.year(), date.month(), date.day(), time.hour(), time.minute()
    )
    now = datetime.now(_SHANGHAI_TZ).replace(tzinfo=None, second=0, microsecond=0)
    if target <= now:
        raise ValueError("定时发布时间必须晚于当前北京时间")
    return target.strftime("%Y-%m-%d %H:%M")


class _LazyMusicComboBox(QComboBox):
    """优先展开已读候选；没有本地缓存时只提示用户刷新。"""

    picker_requested = pyqtSignal()
    candidate_list_requested = pyqtSignal()

    def showPopup(self) -> None:  # noqa: N802 - Qt 固定方法名
        has_candidates = any(
            isinstance(self.itemData(index), dict)
            for index in range(self.count())
        )
        if not has_candidates:
            self.picker_requested.emit()
            return
        self.candidate_list_requested.emit()


class DouyinCommerceConfirmDialog(QDialog):
    """最终提交前的固定摘要确认，打开弹窗本身不触发平台操作。"""

    def __init__(self, summary: dict[str, str], *, enable_timer: bool, parent=None) -> None:
        super().__init__(parent)
        action_label = "确认定时提交" if enable_timer else "确认立即发表"
        schedule_label = "北京时间定时" if enable_timer else "立即发表"
        self.setWindowTitle(action_label)
        self.resize(630, 500)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)

        heading = QLabel(f"确认抖音带货{action_label}")
        heading.setObjectName("dialogTitle")
        layout.addWidget(heading)
        note = QLabel(
            "将复用本次上传后的同一抖音编辑会话。最终提交前会再次回读账号、音乐、"
            f"完整地点、作品内容声明和{schedule_label}；二维码、验证码或未知提示会前台停住。"
        )
        note.setObjectName("infoCallout")
        note.setWordWrap(True)
        layout.addWidget(note)

        panel = QFrame()
        panel.setProperty("subPanel", True)
        panel.setProperty("douyinCommerceCard", True)
        grid = QGridLayout(panel)
        grid.setContentsMargins(14, 12, 14, 12)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(10)
        for row, (label_text, key) in enumerate(
            (
                ("抖音账号", "account"),
                ("视频素材", "video"),
                ("标题", "title"),
                ("用户所选音乐", "music"),
                ("发布位置", "location"),
                ("作品内容声明", "declaration"),
                ("发布方式", "schedule"),
            )
        ):
            label = QLabel(label_text)
            label.setProperty("role", "caption")
            value = QLabel(summary.get(key) or "")
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(label, row, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(value, row, 1)
        grid.setColumnStretch(1, 1)
        layout.addWidget(panel)

        self.confirm_checkbox = QCheckBox(
            f"我已核对账号、视频、音乐、完整地点、作品内容声明与{schedule_label}"
        )
        self.confirm_checkbox.toggled.connect(self._sync_confirm)
        layout.addWidget(self.confirm_checkbox)

        actions = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        actions.button(QDialogButtonBox.StandardButton.Cancel).setText("返回修改")
        actions.button(QDialogButtonBox.StandardButton.Ok).setText(action_label)
        self.confirm_button = actions.button(QDialogButtonBox.StandardButton.Ok)
        self.confirm_button.setProperty("variant", "primary")
        self.confirm_button.setEnabled(False)
        actions.accepted.connect(self.accept)
        actions.rejected.connect(self.reject)
        layout.addWidget(actions)

    def _sync_confirm(self, checked: bool) -> None:
        self.confirm_button.setEnabled(bool(checked))


class DouyinCommerceBatchConfirmDialog(QDialog):
    """批量带货的最终总确认。

    这个对话框只展示客户端已选择的非敏感摘要。它不读取浏览器、不创建平台草稿，
    也不会把地点简称或推断的定时当作平台回读。
    """

    def __init__(self, rows: list[dict[str, str]], *, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("确认批量提交")
        self.resize(760, 560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)
        title = QLabel(f"确认提交 {len(rows)} 条抖音带货视频")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)
        note = QLabel("提交后会逐条在无头会话中重新搜索并精确回读地点、音乐、声明和发布方式。验证码或未知提示会暂停整批。")
        note.setWordWrap(True)
        note.setProperty("role", "caption")
        layout.addWidget(note)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        rows_layout = QVBoxLayout(body)
        rows_layout.setContentsMargins(0, 0, 0, 0)
        rows_layout.setSpacing(8)
        for row in rows:
            card = QFrame()
            card.setProperty("subPanel", True)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(14, 11, 14, 11)
            card_layout.setSpacing(4)
            heading = QLabel(f"第 {row.get('index', '')} 条 · {row.get('video', '')}")
            heading.setObjectName("douyinCommerceBatchConfirmHeading")
            heading.setWordWrap(True)
            location = QLabel(f"地点：{row.get('location', '')}")
            location.setWordWrap(True)
            schedule = QLabel(f"发布方式：{row.get('schedule', '')}")
            schedule.setWordWrap(True)
            card_layout.addWidget(heading)
            card_layout.addWidget(location)
            card_layout.addWidget(schedule)
            rows_layout.addWidget(card)
        rows_layout.addStretch(1)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)

        self.confirm_checkbox = QCheckBox("我已核对全部视频、完整地点和每条发布方式")
        layout.addWidget(self.confirm_checkbox)
        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok)
        actions.button(QDialogButtonBox.StandardButton.Cancel).setText("返回修改")
        actions.button(QDialogButtonBox.StandardButton.Ok).setText("确认批量提交")
        self.confirm_button = actions.button(QDialogButtonBox.StandardButton.Ok)
        self.confirm_button.setProperty("variant", "primary")
        self.confirm_button.setEnabled(False)
        self.confirm_checkbox.toggled.connect(self.confirm_button.setEnabled)
        actions.accepted.connect(self.accept)
        actions.rejected.connect(self.reject)
        layout.addWidget(actions)


class DouyinCommerceBatchResumeConfirmDialog(QDialog):
    """续发前的独立最终确认，明确排除已失败与已完成视频。"""

    def __init__(self, plan: dict, *, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("确认继续发布")
        self.resize(720, 500)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)
        count = int(plan.get("pendingCount") or 0)
        layout.addWidget(QLabel(f"继续发布 {count} 条抖音带货视频"))
        source = QLabel(f"来源任务：{plan.get('sourceTaskNo') or '—'}")
        source.setProperty("role", "caption")
        layout.addWidget(source)
        indexes = "、".join(str(value) for value in plan.get("itemIndexes") or [])
        layout.addWidget(QLabel(f"仅继续原批次第 {indexes} 条；已成功和失败视频不会重试。"))
        note = QLabel("将重新打开浏览器并重新校验音乐、地点、声明和定时。原定时时间过期会安全停止，不会自动改期。")
        note.setWordWrap(True)
        note.setProperty("role", "caption")
        layout.addWidget(note)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        rows = QVBoxLayout(body)
        for index, item in zip(plan.get("itemIndexes") or [], plan.get("batch", {}).get("items") or []):
            card = QFrame()
            card.setProperty("subPanel", True)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(14, 10, 14, 10)
            card_layout.addWidget(QLabel(f"第 {index} 条 · {Path(str(item.get('mediaPath') or '')).name}"))
            schedule = str(item.get("scheduleTime") or "").strip()
            card_layout.addWidget(QLabel(f"发布方式：北京时间定时 {schedule}" if schedule else "发布方式：立即发布"))
            rows.addWidget(card)
        rows.addStretch(1)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)
        self.confirm_checkbox = QCheckBox("我已核对待续发视频及原定时时间")
        layout.addWidget(self.confirm_checkbox)
        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok)
        actions.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        actions.button(QDialogButtonBox.StandardButton.Ok).setText(f"确认继续发布 {count} 条")
        self.confirm_button = actions.button(QDialogButtonBox.StandardButton.Ok)
        self.confirm_button.setEnabled(False)
        self.confirm_checkbox.toggled.connect(self.confirm_button.setEnabled)
        actions.accepted.connect(self.accept)
        actions.rejected.connect(self.reject)
        layout.addWidget(actions)


class DouyinCommercePage(QWidget):
    """单账号、单视频的抖音带货分步向导。"""

    request_account_management = pyqtSignal()

    # 用户先在本机准备内容，再进入隔离采集页。音乐、定位、声明可任意先后选择；
    # 本页仅本地暂存，正式发布时才逐条重新核验并应用。
    _STEPS = ("内容准备", "平台设置", "检查与提交")
    _STEP_HINTS = (
        "选择账号、视频并完成本地内容准备。",
        "在独立临时采集会话读取候选；本页仅本机暂存，正式发布时逐条重新核验。",
        "只读预检通过后，再进行一次独立提交确认。",
    )
    _PLATFORM_STAGE_ORDER = ("blocked", "music", "location", "declaration", "schedule")
    _IMMEDIATE_WRITE_KEY = "douyin_commerce_immediate_write"
    _COLLECTOR_TASK_KEY = "douyin_commerce_collector_action"
    _SETUP_GENERATION_TASK_KEY = "douyin_commerce_setup_generation"
    _SETUP_GENERATION_CLOSE_TASK_KEY = "douyin_commerce_setup_generation_close"
    _COLLECTOR_BARRIER_ERROR = "平台设置临时会话未完全关闭，已安全停止"
    _SHUTDOWN_WAIT_SECONDS = 5.0
    _DEFAULT_CONTENT_DECLARATION = "无需添加自主声明"
    _DISABLED_CONTENT_DECLARATIONS = frozenset({"内容为转载信息"})
    _BATCH_EDITOR_SESSION_ENDED_HINT = "编辑会话已结束；预检将为每条视频重新建立上传会话"
    _UPLOAD_PROGRESS = {
        "checking_session": (1, "正在检查账号"),
        "opening_editor": (2, "正在打开上传会话"),
        "uploading_video": (3, "正在上传视频"),
        "reading_content": (4, "正在读取标题和文案"),
        "waiting_platform": (5, "正在等待平台处理"),
        "ready": (6, "上传完成"),
        "syncing_content": (4, "正在同步内容"),
        "content_synced": (6, "内容同步完成"),
        "sync_failed": (0, "内容同步未完成"),
    }
    _STAGE_ERROR_COPY = {
        "content": (
            "本机内容尚未形成可上传条件。",
            "核对账号、视频和作品文案后重新上传。",
        ),
        "music": (
            "平台未返回可确认的收藏音乐。",
            "回到音乐步骤，重新读取后手动选择一首音乐。",
        ),
        "location": (
            "平台未返回可唯一确认的完整地点。",
            "核对范围和关键词后重新搜索，再确认完整地址。",
        ),
        "declaration": (
            "平台未回读与所选值一致的作品内容声明。",
            "关闭当前说明浮层后，重新选择并确认声明。",
        ),
        "schedule": (
            "发布时间未满足当前设置条件。",
            "选择立即发表，或填写未来的北京时间。",
        ),
    }

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("pageRoot")
        self.runner = BackgroundTaskRunner(self)
        self._session_id = ""
        # 平台设置采集代际与正式发布会话严格分离。这里永远不保存三个
        # 采集器内部的 sessionId，只保留协调器公开的代际和实例状态。
        self._setup_generation_id = ""
        self._setup_generation_cleanup_required = False
        self._shutdown_requested = threading.Event()
        self._collector_status: dict[str, object] = {}
        self._last_failed_collector_type = ""
        self._staged_music_confirmed = False
        self._staged_location_confirmed = False
        self._staged_declaration_confirmed = False
        self._setup_start_token = 0
        self._setup_close_token = 0
        self._setup_start_error_code = ""
        self._setup_close_error_code = ""
        self._collector_action_tokens = {
            "domestic_location": 0,
            "favorite_music": 0,
            "local_location": 0,
        }
        self._platform_collector_progress_owner: tuple[str, str, int] | None = None
        self._platform_collector_progress_label_text = ""
        self._platform_collector_progress_started = 0.0
        self._platform_collector_progress_timer = QTimer(self)
        self._platform_collector_progress_timer.setInterval(1_000)
        self._platform_collector_progress_timer.timeout.connect(
            self._update_platform_collector_progress
        )
        self._music_candidates: list[dict[str, str]] = []
        self._music_candidate_source = ""
        self._selected_music: dict[str, str] | None = None
        self._pending_music: dict[str, str] | None = None
        self._open_music_picker_after_load = False
        self._cache_load_pending = False
        self._locations: list[dict[str, str]] = []
        # 候选列表会在用户确认后收起；因此已选地点不能依赖 QListWidget 的选中态。
        self._selected_location_data: dict[str, str] | None = None
        self._pending_location: dict[str, str] | None = None
        self._location_applied = False
        self._declaration_applied = False
        self._confirmed_declaration = ""
        self._pending_declaration = ""
        self._immediate_write_kind = ""
        self._suppress_declaration_signal = True
        self._preflight_fingerprint = ""
        self._active_task_id: int | None = None
        self._douyin_verification_dialog: DouyinVerificationDialog | None = None
        self._douyin_verification_request_id = ""
        self._verification_item_context: dict[str, object] = {}
        self._douyin_verification_poll_timer = QTimer(self)
        self._douyin_verification_poll_timer.setInterval(250)
        self._douyin_verification_poll_timer.timeout.connect(
            self._poll_douyin_verification
        )
        # 上传快照只用于区分“内容同步”和“重新上传”，不保存平台会话或凭据。
        self._uploaded_editor_payload: dict | None = None
        self._pending_upload_payload: dict | None = None
        self._saved_content_available = False
        # 单条与批量内容分别保存到不同的本地草稿表；两者的恢复入口不能共用
        # 一个可用状态，否则清空表单或重启后会把批量草稿误判为不存在。
        self._batch_saved_content_available = False
        self._tag_values: list[str] = []
        self._tag_history: list[str] = []
        # 批量工作台在内容准备后保留一个无头“设置会话”：它只用于读取当前
        # 账号的收藏音乐和地点候选，不保存草稿也不提交。每条视频的地点选择
        # 以完整 POI 身份保存为本地预设，预检/提交时再逐条重新搜索并精确回读。
        self._selected_video_indexes: list[int] = []
        self._batch_locations: dict[str, dict[str, object]] = {}
        self._batch_location_searches: dict[str, dict[str, object]] = {}
        self._batch_location_feedback = ""
        self._batch_item_rows_signature: tuple[object, ...] | None = None
        self._batch_schedule_overrides: dict[str, str] = {}
        # 批量任务和旧的单条任务必须分开保存；验证轮询会优先使用仍在运行的
        # 批量任务号，确保原生短信/二维码对话框与当前视频保持同一会话。
        self._batch_task_id: int | None = None
        self._batch_preflight_fingerprint = ""
        self._batch_progress_text = ""
        self._batch_result_feedback = ""
        # 批次结果仍需留在当前页面供复制；只有用户真正进入下一批平台设置时
        # 才清空共享执行日志，避免多批记录混在一起。
        self._clear_runtime_log_on_next_task = False
        self._batch_pause_requested = False
        # 只自动维护“系统默认”日期；用户手动选择的未来日期绝不覆盖。
        self._schedule_date_auto_default = True
        # 批量执行器会在每条视频完成后主动关闭无头编辑会话。这个标记只用于
        # 向用户解释“为什么共享文字改完后要在下次预检重建会话”，不保存会话信息。
        self._batch_editor_session_ended = False
        self._batch_executor = DouyinCommerceBatchExecutor()
        self._stage_error_labels: dict[str, QLabel] = {}
        self._commerce_progress_phase = ""
        self._build_ui()
        self._suppress_declaration_signal = False
        self.refresh()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(48, 24, 48, 24)
        root.setSpacing(10)

        # 内容准备必须与已确认的三栏桌面稿保持同一视觉合同。这里不复用旧的
        # "命令栏 + 流程说明" 组合，避免用户再次看到只是换文案的旧页面。
        header = QFrame()
        header.setObjectName("douyinCommerceReferenceHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(12)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        title = QLabel("抖音带货")
        title.setObjectName("douyinCommerceReferenceTitle")
        subtitle = QLabel("一次上传，上传后再设置。")
        subtitle.setObjectName("douyinCommerceReferenceSubtitle")
        subtitle.setWordWrap(True)
        titles.addWidget(title)
        titles.addWidget(subtitle)
        # 三步条和底部主操作已说明流程，副标题不再重复占据首屏高度。
        subtitle.setVisible(False)
        header_layout.addLayout(titles, 1)

        header_actions = QHBoxLayout()
        header_actions.setSpacing(10)
        self.reference_save_badge = QLabel("本地内容未保存")
        self.reference_save_badge.setObjectName("douyinCommerceReferenceSaveBadge")
        self.reference_save_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_actions.addWidget(self.reference_save_badge)
        self.abandon_button = button("放弃本次上传", variant="secondary", compact=True)
        self.abandon_button.setObjectName("douyinCommerceAbandon")
        self.abandon_button.clicked.connect(self.abandon_session)
        header_actions.addWidget(self.abandon_button)
        header_layout.addLayout(header_actions)

        # 下列三个对象保留给既有状态机和回归测试；它们不再占据首屏视觉空间。
        self.status_badge = QLabel("等待内容准备", header)
        self.status_badge.setVisible(False)
        self.hero_detail_label = QLabel("尚未开始平台操作", header)
        self.hero_detail_label.setVisible(False)
        self.session_hint_label = QLabel("本机内容准备", header)
        self.session_hint_label.setVisible(False)
        root.addWidget(header)

        progress_panel = QFrame()
        progress_panel.setObjectName("douyinCommerceProgress")
        progress_layout = QVBoxLayout(progress_panel)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        progress_layout.setSpacing(0)
        self.step_row = QHBoxLayout()
        self.step_row.setSpacing(8)
        self.step_labels = []
        self.step_cards: list[QFrame] = []
        for index, label_text in enumerate(self._STEPS):
            step_card = QFrame()
            step_card.setObjectName("douyinCommerceStepCard")
            step_card.setMinimumHeight(48)
            step_card.setProperty("stepState", "pending")
            step_layout = QHBoxLayout(step_card)
            step_layout.setContentsMargins(14, 8, 14, 8)
            step_layout.setSpacing(8)
            step_number = QLabel(str(index + 1))
            step_number.setObjectName("douyinCommerceStepNumber")
            step_number.setAlignment(Qt.AlignmentFlag.AlignCenter)
            step_label = QLabel(label_text)
            step_label.setObjectName("douyinCommerceStep")
            step_label.setAlignment(Qt.AlignmentFlag.AlignVCenter)
            step_label.setProperty("stepState", "pending")
            step_label.setToolTip("进度提示；请使用本页的返回或继续按钮切换步骤。")
            step_layout.addWidget(step_number)
            step_layout.addWidget(step_label, 1)
            self.step_row.addWidget(step_card, 1)
            self.step_labels.append(step_label)
            self.step_cards.append(step_card)
        progress_layout.addLayout(self.step_row)
        self.progress_context_label = QLabel(self._STEP_HINTS[0])
        self.progress_context_label.setObjectName("douyinCommerceProgressContext")
        self.progress_context_label.setWordWrap(True)
        self.progress_context_label.setVisible(False)
        progress_layout.addWidget(self.progress_context_label)
        root.addWidget(progress_panel)

        stage_switcher = QFrame()
        stage_switcher.setObjectName("douyinCommerceStageSwitcher")
        stage_layout = QHBoxLayout(stage_switcher)
        stage_layout.setContentsMargins(0, 0, 0, 0)
        stage_layout.setSpacing(8)
        self.content_stage_button = button("内容准备", variant="secondary", compact=True)
        self.content_stage_button.setObjectName("douyinCommerceStageButton")
        self.content_stage_button.clicked.connect(lambda: self._go_to_step(0))
        self.platform_stage_button = button("上传后平台设置", variant="secondary", compact=True)
        self.platform_stage_button.setObjectName("douyinCommerceStageButton")
        self.platform_stage_button.clicked.connect(lambda: self._go_to_step(1))
        stage_layout.addWidget(self.content_stage_button)
        stage_layout.addWidget(self.platform_stage_button)
        stage_layout.addStretch(1)
        # 顶部步骤已足以表达进度，这组重复导航保留给状态机但不占据用户注意力。
        stage_switcher.setVisible(False)
        root.addWidget(stage_switcher)

        # 内容准备页的主要命令固定在窗口底部。填写文案后不用再滚回表单底部
        # 寻找上传按钮；上传后的平台设置页则使用各自语义明确的当前操作按钮。
        self.operation_dock = self._build_operation_dock()
        self.pages = QStackedWidget()
        self.pages.setObjectName("douyinCommerceWizard")
        self.pages.addWidget(self._build_content_page())
        self.pages.addWidget(self._build_platform_settings_page())
        self.pages.addWidget(self._build_review_page())

        # 任务摘要只在最后的检查页集中展示。常驻侧栏会和当前操作竞争注意力，
        # 也会把“待完成”误读成已配置的事实。
        root.addWidget(self.pages, 1)
        root.addWidget(self.operation_dock)

    @staticmethod
    def _scroll_page(body: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setObjectName("douyinCommerceStepScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(body)
        return scroll

    @staticmethod
    def _section(title_text: str, helper: str) -> tuple[QFrame, QVBoxLayout]:
        panel = QFrame()
        panel.setProperty("subPanel", True)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(10)
        title = QLabel(title_text)
        title.setObjectName("sectionTitle")
        helper_label = QLabel(helper)
        helper_label.setProperty("role", "caption")
        helper_label.setWordWrap(True)
        layout.addWidget(title)
        if _normalized(helper):
            layout.addWidget(helper_label)
        return panel, layout

    @staticmethod
    def _reference_column(
        object_name: str, title_text: str
    ) -> tuple[QFrame, QVBoxLayout]:
        """构建内容准备页的固定三栏卡片，不复用平台设置页的通用分段样式。"""

        panel = QFrame()
        panel.setObjectName(object_name)
        panel.setMinimumHeight(510)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 18)
        layout.setSpacing(14)

        head = QFrame()
        head.setObjectName("douyinCommerceReferenceCardHead")
        head_layout = QVBoxLayout(head)
        head_layout.setContentsMargins(22, 18, 22, 16)
        title = QLabel(title_text)
        title.setObjectName("douyinCommerceReferenceCardEyebrow")
        head_layout.addWidget(title)
        layout.addWidget(head)
        return panel, layout

    def _build_operation_dock(self) -> QFrame:
        """创建仅在本机内容准备阶段出现的固定主操作栏。"""

        dock = QFrame()
        dock.setObjectName("douyinCommerceReferenceFooter")
        layout = QHBoxLayout(dock)
        self._configure_reference_footer(dock, layout)
        copy = QVBoxLayout()
        copy.setSpacing(4)
        title = QLabel("内容准备")
        title.setObjectName("douyinCommerceOperationTitle")
        self.operation_dock_detail = QLabel(
            "内容已保存。确认账号、文案和视频后，才会打开一次抖音编辑会话。"
        )
        self.operation_dock_detail.setObjectName("douyinCommerceOperationDetail")
        self.operation_dock_detail.setWordWrap(True)
        copy.addWidget(title)
        copy.addWidget(self.operation_dock_detail)
        layout.addLayout(copy, 1)
        self.operation_progress_frame = QFrame()
        self.operation_progress_frame.setObjectName("douyinCommerceUploadProgress")
        progress_layout = QVBoxLayout(self.operation_progress_frame)
        progress_layout.setContentsMargins(10, 7, 10, 7)
        progress_layout.setSpacing(4)
        self.operation_progress_label = QLabel("正在准备上传")
        self.operation_progress_label.setObjectName("douyinCommerceUploadProgressLabel")
        self.operation_progress = QProgressBar()
        self.operation_progress.setObjectName("douyinCommerceUploadProgressBar")
        self.operation_progress.setRange(0, len(self._UPLOAD_PROGRESS))
        self.operation_progress.setValue(0)
        self.operation_progress.setTextVisible(False)
        self.operation_progress.setFixedWidth(180)
        progress_layout.addWidget(self.operation_progress_label)
        progress_layout.addWidget(self.operation_progress)
        self.operation_progress_frame.setVisible(False)
        layout.addWidget(self.operation_progress_frame)
        self.operation_dock_status = QLabel("待补全")
        self.operation_dock_status.setObjectName("douyinCommerceOperationStatus")
        layout.addWidget(self.operation_dock_status)
        self.operation_dock_upload = button("上传视频并继续", variant="primary")
        self.operation_dock_upload.setObjectName("douyinCommerceUpload")
        self.operation_dock_upload.setProperty("footerAction", True)
        self.operation_dock_upload.clicked.connect(self.continue_after_content)
        layout.addWidget(self.operation_dock_upload)
        return dock

    @staticmethod
    def _configure_reference_footer(dock: QFrame, layout: QHBoxLayout) -> None:
        """统一三个阶段底部操作栏的尺寸与排版。"""

        dock.setFixedHeight(82)
        layout.setContentsMargins(22, 15, 20, 15)
        layout.setSpacing(12)

    def _build_content_page(self) -> QWidget:
        body = QWidget()
        body.setObjectName("douyinCommerceStepBody")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 8)
        layout.setSpacing(0)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        workspace = QFrame()
        workspace.setObjectName("douyinCommerceThreeColumn")
        columns = QGridLayout(workspace)
        self.content_columns = columns
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setHorizontalSpacing(20)
        columns.setVerticalSpacing(0)

        account_panel, account_layout = self._reference_column(
            "douyinCommerceContentAccountColumn",
            "账号",
        )
        account_panel.setProperty("douyinCommerceReferenceColumn", True)
        account_layout.setContentsMargins(18, 0, 18, 18)
        account_label = QLabel("选择账号")
        account_label.setObjectName("douyinCommerceFieldLabel")
        account_layout.addWidget(account_label)
        self.account_combo = QComboBox()
        self.account_combo.setObjectName("douyinCommerceAccount")
        self.account_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.account_combo.setIconSize(QSize(26, 26))
        self.account_combo.setMinimumHeight(44)
        self.account_combo.setToolTip("从已绑定的抖音账号中选择")
        self.account_combo.currentIndexChanged.connect(self._content_changed)
        account_layout.addWidget(self.account_combo)

        self.account_identity_card = QFrame()
        self.account_identity_card.setObjectName("douyinCommerceAccountIdentityCard")
        identity_layout = QHBoxLayout(self.account_identity_card)
        identity_layout.setContentsMargins(16, 14, 16, 14)
        identity_layout.setSpacing(12)
        self.account_avatar = QLabel()
        self.account_avatar.setObjectName("douyinCommerceAccountAvatar")
        self.account_avatar.setFixedSize(_ACCOUNT_AVATAR_SIZE, _ACCOUNT_AVATAR_SIZE)
        self.account_avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        identity_layout.addWidget(self.account_avatar)
        identity_copy = QVBoxLayout()
        identity_copy.setSpacing(4)
        self.account_identity_name = QLabel("尚未选择抖音账号")
        self.account_identity_name.setObjectName("douyinCommerceAccountIdentityName")
        self.account_card = QLabel("选择一个状态正常的抖音账号")
        self.account_card.setObjectName("douyinCommerceAccountCard")
        self.account_card.setWordWrap(True)
        identity_copy.addWidget(self.account_identity_name)
        identity_copy.addWidget(self.account_card)
        identity_layout.addLayout(identity_copy, 1)
        account_layout.addWidget(self.account_identity_card)

        video_label = QLabel("视频")
        video_label.setObjectName("douyinCommerceFieldLabel")
        account_layout.addWidget(video_label)
        self.video_combo = QComboBox()
        self.video_combo.setObjectName("douyinCommerceVideo")
        self.video_combo.currentIndexChanged.connect(self._content_changed)
        self.video_combo.setVisible(False)
        account_layout.addWidget(self.video_combo)

        self.video_preview = QFrame()
        self.video_preview.setObjectName("douyinCommerceVideoPreview")
        preview_layout = QHBoxLayout(self.video_preview)
        preview_layout.setContentsMargins(16, 16, 16, 16)
        preview_layout.setSpacing(14)
        self.video_thumbnail = QLabel("视频")
        self.video_thumbnail.setObjectName("douyinCommerceVideoThumbnail")
        self.video_thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_thumbnail.setFixedSize(96, 142)
        self.video_thumbnail.setProperty("hasPreview", False)
        preview_layout.addWidget(self.video_thumbnail)
        self.video_card = QLabel("尚未选择视频素材")
        self.video_card.setObjectName("douyinCommerceVideoCard")
        self.video_card.setWordWrap(True)
        self.video_card.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        preview_layout.addWidget(self.video_card, 1)
        account_layout.addWidget(self.video_preview)

        replace_wrap = QFrame()
        replace_wrap.setObjectName("douyinCommerceVideoReplace")
        replace_layout = QHBoxLayout(replace_wrap)
        replace_layout.setContentsMargins(8, 4, 8, 4)
        self.video_replace_button = button("选择视频", variant="secondary", compact=True)
        self.video_replace_button.setObjectName("douyinCommerceVideoReplaceButton")
        self.video_replace_button.clicked.connect(self._open_video_picker)
        replace_layout.addWidget(self.video_replace_button, 1)
        account_layout.addWidget(replace_wrap)
        self.video_notice = QLabel("上传后设置音乐、地点、声明和定时")
        self.video_notice.setObjectName("douyinCommerceVideoNotice")
        self.video_notice.setWordWrap(True)
        account_layout.addWidget(self.video_notice)

        self.login_required_frame = QFrame()
        self.login_required_frame.setObjectName("douyinCommerceLoginRequired")
        login_layout = QVBoxLayout(self.login_required_frame)
        login_layout.setContentsMargins(12, 10, 12, 10)
        login_layout.setSpacing(8)
        self.login_required_label = QLabel("登录已失效，请到账号管理重新登录")
        self.login_required_label.setObjectName("douyinCommerceLoginRequiredText")
        self.login_required_label.setWordWrap(True)
        login_layout.addWidget(self.login_required_label)
        self.login_required_button = button("前往账号管理", variant="secondary", compact=True)
        self.login_required_button.setObjectName("douyinCommerceGoToAccountManagement")
        self.login_required_button.clicked.connect(self.request_account_management.emit)
        login_layout.addWidget(self.login_required_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.login_required_frame.setVisible(False)
        account_layout.addWidget(self.login_required_frame)
        account_layout.addStretch(1)
        columns.addWidget(account_panel, 0, 0)

        content_panel, content_layout = self._reference_column(
            "douyinCommerceContentBodyColumn",
            "内容",
        )
        content_panel.setProperty("douyinCommerceReferenceColumn", True)
        content_layout.setContentsMargins(22, 0, 22, 18)
        title_label = QLabel("标题（选填）")
        title_label.setObjectName("douyinCommerceFieldLabel")
        content_layout.addWidget(title_label)
        self.title_input = QLineEdit()
        self.title_input.setObjectName("douyinCommerceTitle")
        self.title_input.setMaxLength(55)
        self.title_input.setPlaceholderText("选填：填写抖音视频标题")
        self.title_input.textChanged.connect(self._content_changed)
        content_layout.addWidget(self.title_input)
        desc_label = QLabel("作品文案")
        desc_label.setObjectName("douyinCommerceFieldLabel")
        content_layout.addWidget(desc_label)
        self.description_input = _ImeAwarePlainTextEdit()
        self.description_input.setObjectName("douyinCommerceDescription")
        self.description_input.setPlaceholderText("填写视频发布文案。上传后会由平台编辑页回读。")
        self.description_input.setFixedHeight(154)
        self.description_input.textChanged.connect(self._content_changed)
        content_layout.addWidget(self.description_input)
        tag_title_row = QHBoxLayout()
        tag_title = QLabel("话题标签")
        tag_title.setObjectName("douyinCommerceFieldLabel")
        tag_title_row.addWidget(tag_title)
        tag_title_row.addStretch()
        tag_hint = QLabel("输入后回车添加")
        tag_hint.setObjectName("douyinCommerceFieldHint")
        tag_title_row.addWidget(tag_hint)
        content_layout.addLayout(tag_title_row)
        tag_entry = QHBoxLayout()
        tag_entry.setSpacing(8)
        self.tags_input = QLineEdit()
        self.tags_input.setObjectName("douyinCommerceTags")
        self.tags_input.setPlaceholderText("例如：探店、团购")
        self.tags_input.textChanged.connect(self._content_changed)
        self.tags_input.returnPressed.connect(self.add_tags_from_input)
        self.tag_add_button = button("添加", variant="secondary", compact=True)
        self.tag_add_button.setObjectName("douyinCommerceAddTag")
        self.tag_add_button.clicked.connect(self.add_tags_from_input)
        tag_entry.addWidget(self.tags_input, 1)
        tag_entry.addWidget(self.tag_add_button)
        content_layout.addLayout(tag_entry)
        self.selected_tags_host = QFrame()
        self.selected_tags_host.setObjectName("douyinCommerceTagHost")
        self.selected_tags_layout = QHBoxLayout(self.selected_tags_host)
        self.selected_tags_layout.setContentsMargins(8, 6, 8, 6)
        self.selected_tags_layout.setSpacing(6)
        content_layout.addWidget(self.selected_tags_host)
        history_title = QLabel("历史标签")
        history_title.setObjectName("douyinCommerceFieldLabel")
        content_layout.addWidget(history_title)
        self.tag_history_host = QFrame()
        self.tag_history_host.setObjectName("douyinCommerceTagHistoryHost")
        self.tag_history_host.setFixedHeight(history_tag_box_height(3))
        self.tag_history_layout = FlowLayout(
            self.tag_history_host,
            contents_margins=(8, 5, 8, 5),
            horizontal_spacing=6,
            vertical_spacing=4,
        )
        content_layout.addWidget(self.tag_history_host)
        self.content_notice = QLabel("保存后再上传；上传后设置音乐、地点、声明和定时。")
        self.content_notice.setObjectName("douyinCommerceInlineNotice")
        self.content_notice.setWordWrap(True)
        self.content_notice.setVisible(False)
        content_layout.addWidget(self.content_notice)
        content_layout.addStretch(1)
        local_copy = QLabel("本地内容")
        local_copy.setObjectName("douyinCommerceFieldLabel")
        content_layout.addWidget(local_copy)
        self.content_save_status = QLabel("尚未保存本地内容")
        self.content_save_status.setObjectName("douyinCommerceContentSaveStatus")
        self.content_save_status.setWordWrap(True)
        content_layout.addWidget(self.content_save_status)
        local_actions = QHBoxLayout()
        self.save_content_button = button("保存本地内容", variant="secondary", compact=True)
        self.save_content_button.setObjectName("douyinCommerceSaveContent")
        self.save_content_button.clicked.connect(self.save_content)
        self.restore_content_button = button("恢复已保存内容", variant="secondary", compact=True)
        self.restore_content_button.setObjectName("douyinCommerceRestoreContent")
        self.restore_content_button.clicked.connect(self.restore_saved_content)
        self.clear_content_button = button("清空当前信息", variant="secondary", compact=True)
        self.clear_content_button.setObjectName("douyinCommerceClearContent")
        self.clear_content_button.setToolTip("清空当前表单；不会删除已保存的本地内容")
        self.clear_content_button.clicked.connect(self.clear_current_content)
        local_actions.addWidget(self.save_content_button)
        local_actions.addWidget(self.restore_content_button)
        local_actions.addWidget(self.clear_content_button)
        local_actions.addStretch(1)
        content_layout.addLayout(local_actions)
        # 批量模式的保存入口位于右侧视频栏底部；这组保留为旧单条会话的
        # 兼容控件，不再争抢内容填写区的注意力。
        local_copy.setVisible(False)
        self.content_save_status.setVisible(False)
        self.save_content_button.setVisible(False)
        self.restore_content_button.setVisible(False)
        self.clear_content_button.setVisible(False)
        content_layout.addWidget(self._stage_error_label("content"))
        columns.addWidget(content_panel, 0, 1)

        # 右栏沿用账号和内容卡片的同一视觉层级，专门承载批量视频选择。
        self.batch_video_panel, video_layout = self._reference_column(
            "douyinCommerceContentVideoColumn", "视频"
        )
        self.batch_video_panel.setProperty("douyinCommerceReferenceColumn", True)
        video_layout.setContentsMargins(18, 0, 18, 18)
        video_selection_actions = QHBoxLayout()
        video_selection_actions.setSpacing(8)
        video_hint = QLabel("选择要使用的视频")
        video_hint.setObjectName("douyinCommerceFieldLabel")
        video_selection_actions.addWidget(video_hint)
        video_selection_actions.addStretch(1)
        self.select_all_videos_button = button("全选", variant="secondary", compact=True)
        self.select_all_videos_button.setObjectName("douyinCommerceSelectAllVideos")
        self.select_all_videos_button.clicked.connect(self.select_all_batch_videos)
        self.clear_video_selection_button = button("取消全选", variant="secondary", compact=True)
        self.clear_video_selection_button.setObjectName("douyinCommerceClearVideoSelection")
        self.clear_video_selection_button.clicked.connect(self.clear_batch_video_selection)
        video_selection_actions.addWidget(self.select_all_videos_button)
        video_selection_actions.addWidget(self.clear_video_selection_button)
        video_layout.addLayout(video_selection_actions)
        self.batch_video_list = QListWidget()
        self.batch_video_list.setObjectName("douyinCommerceBatchVideoList")
        self.batch_video_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.batch_video_list.setMinimumHeight(250)
        self.batch_video_list.itemChanged.connect(self._batch_video_item_changed)
        video_layout.addWidget(self.batch_video_list, 1)
        self.batch_video_status = QLabel("可选择 1 至 20 条视频")
        self.batch_video_status.setObjectName("douyinCommerceBatchVideoStatus")
        self.batch_video_status.setWordWrap(True)
        video_layout.addWidget(self.batch_video_status)
        self.batch_save_content_button = button("保存本地内容", variant="secondary", compact=True)
        self.batch_save_content_button.setObjectName("douyinCommerceSaveBatchContent")
        self.batch_save_content_button.clicked.connect(self.save_batch_content)
        video_layout.addWidget(self.batch_save_content_button)
        self.batch_restore_content_button = button("恢复已保存内容", variant="secondary", compact=True)
        self.batch_restore_content_button.setObjectName("douyinCommerceRestoreBatchContent")
        self.batch_restore_content_button.clicked.connect(self.restore_batch_content)
        video_layout.addWidget(self.batch_restore_content_button)
        self.batch_clear_content_button = button("清空当前内容", variant="secondary", compact=True)
        self.batch_clear_content_button.setObjectName("douyinCommerceClearBatchContent")
        self.batch_clear_content_button.setToolTip("清空当前表单；不会删除已保存的本地内容")
        self.batch_clear_content_button.clicked.connect(self.clear_current_content)
        video_layout.addWidget(self.batch_clear_content_button)
        self.content_execution_log = ExecutionLogPanel()
        self.content_execution_log.setMinimumHeight(510)
        self.content_execution_log.setVisible(False)
        video_layout.addWidget(self.content_execution_log)
        columns.addWidget(self.batch_video_panel, 0, 2)
        columns.setColumnStretch(0, 31)
        columns.setColumnStretch(1, 42)
        columns.setColumnStretch(2, 27)
        columns.setColumnMinimumWidth(0, 300)
        columns.setColumnMinimumWidth(2, 300)
        layout.addWidget(workspace)

        # 固定操作栏是内容准备页唯一的主操作入口，避免把上传按钮藏在长表单末尾。
        self.upload_button = self.operation_dock_upload
        return self._scroll_page(body)

    def _select_local_video(self, index: int) -> bool:
        """切换本机视频选择；无效索引不得改变当前素材或创建平台会话。"""

        if not 1 <= index < self.video_combo.count():
            return False
        self.video_combo.setCurrentIndex(index)
        return True

    def _open_video_picker(self) -> None:
        """在本机素材列表中更换本次唯一视频，不读取或操作平台页面。"""

        if self.video_combo.count() <= 1:
            QMessageBox.information(self, "选择视频", "素材管理中暂未找到可选视频。")
            return
        menu = self._build_video_picker_menu()
        menu.exec(self._video_picker_position(menu))

    def _build_video_picker_menu(self) -> QMenu:
        """构建受客户端边界约束的本地视频选择菜单。"""

        client = self.window()
        available_width = max(1, client.width() - 32)
        available_height = max(1, client.height() - 32)
        menu_width = min(_VIDEO_PICKER_MAX_WIDTH, available_width)
        menu_height = min(_VIDEO_PICKER_MAX_HEIGHT, available_height)
        menu = QMenu(self)
        menu.setObjectName("douyinCommerceVideoPickerMenu")
        menu.setFixedSize(menu_width, menu_height)

        scroll = QScrollArea(menu)
        scroll.setObjectName("douyinCommerceVideoPickerScroll")
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFixedSize(max(1, menu_width - 12), max(1, menu_height - 12))
        content = QWidget()
        content.setObjectName("douyinCommerceVideoPickerRows")
        rows_layout = QVBoxLayout(content)
        rows_layout.setContentsMargins(2, 2, 2, 2)
        rows_layout.setSpacing(4)
        for index in range(1, self.video_combo.count()):
            media = self.video_combo.itemData(index)
            media_data = dict(media) if isinstance(media, dict) else {}
            full_name = self.video_combo.itemText(index)
            row = QPushButton(self._video_picker_title(full_name))
            row.setObjectName("douyinCommerceVideoPickerItem")
            row.setCheckable(True)
            row.setChecked(index == self.video_combo.currentIndex())
            row.setIcon(self._video_picker_icon(media_data))
            row.setIconSize(QSize(48, 60))
            row.setFixedHeight(_VIDEO_PICKER_ROW_HEIGHT)
            row.setToolTip(full_name)
            row.setAccessibleName(f"选择视频：{full_name}")
            row.clicked.connect(
                lambda _checked=False, selected_index=index: self._choose_video_from_menu(
                    menu, selected_index
                )
            )
            rows_layout.addWidget(row)
        rows_layout.addStretch(1)
        scroll.setWidget(content)
        action = QWidgetAction(menu)
        action.setDefaultWidget(scroll)
        menu.addAction(action)
        return menu

    @staticmethod
    def _video_picker_title(full_name: str) -> str:
        """限制下拉行的文件名长度，完整名称保留在悬停提示。"""

        normalized = _normalized(full_name)
        if len(normalized) <= _VIDEO_PICKER_NAME_LIMIT:
            return normalized
        return f"{normalized[: _VIDEO_PICKER_NAME_LIMIT - 1]}…"

    @staticmethod
    def _video_picker_icon(media: dict) -> QIcon:
        """读取本地素材封面；没有封面时给出纯本机占位缩略图。"""

        cover_path = ""
        try:
            cover_path = media_service.cover_display_path(media)
        except Exception:
            _LOGGER.exception("读取本机视频选择菜单封面失败")
        pixmap = QPixmap(cover_path) if cover_path else QPixmap()
        if not pixmap.isNull():
            return QIcon(pixmap)

        placeholder = QPixmap(48, 60)
        placeholder.fill(QColor("#526A80"))
        painter = QPainter(placeholder)
        painter.setPen(Qt.GlobalColor.white)
        font = painter.font()
        font.setBold(True)
        font.setPixelSize(12)
        painter.setFont(font)
        painter.drawText(
            placeholder.rect(), Qt.AlignmentFlag.AlignCenter, "视频"
        )
        painter.end()
        return QIcon(placeholder)

    def _choose_video_from_menu(self, menu: QMenu, index: int) -> None:
        """选择本机视频后关闭菜单，不会创建平台会话。"""

        self._select_local_video(index)
        menu.close()

    def _video_picker_position(self, menu: QMenu) -> QPoint:
        """把素材菜单限制在当前客户端窗口内，而非让长菜单越过应用边界。"""

        client = self.window()
        client_origin = client.mapToGlobal(QPoint(0, 0))
        client_left = client_origin.x()
        client_top = client_origin.y()
        client_right = client_left + client.width()
        client_bottom = client_top + client.height()
        anchor = self.video_replace_button.mapToGlobal(
            self.video_replace_button.rect().bottomLeft()
        )
        horizontal_margin = 16
        vertical_margin = 16
        x = max(
            client_left + horizontal_margin,
            min(anchor.x(), client_right - menu.width() - horizontal_margin),
        )
        y = anchor.y()
        if y + menu.height() > client_bottom - vertical_margin:
            y = max(
                client_top + vertical_margin,
                self.video_replace_button.mapToGlobal(
                    self.video_replace_button.rect().topLeft()
                ).y()
                - menu.height(),
            )
        return QPoint(x, y)

    def _build_platform_settings_page(self) -> QWidget:
        """构建上传后的双栏设置页，保留真实平台顺序而不轮流占满整页。"""

        body = QWidget()
        body.setObjectName("douyinCommerceStepBody")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 14, 8)
        layout.setSpacing(12)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # 平台状态统一投射到底部操作栏，避免在工作区顶部重复占用一条提示栏。
        # 保留这个隐藏标签作为状态数据源，兼容现有流程与测试入口。
        self.platform_session_status = QLabel("等待上传视频", body)
        self.platform_session_status.setObjectName("douyinCommercePlatformProgressText")
        self.platform_session_status.setVisible(False)
        self.platform_back_button = button("返回内容", variant="secondary", compact=True)
        self.platform_back_button.setObjectName("douyinCommerceBackToContent")
        self.platform_back_button.clicked.connect(lambda: self._go_to_step(0))

        collector_status_bar = QFrame()
        collector_status_bar.setObjectName("douyinCommerceCollectorStatusBar")
        collector_status_layout = QHBoxLayout(collector_status_bar)
        collector_status_layout.setContentsMargins(14, 9, 14, 9)
        collector_status_layout.setSpacing(14)
        self.domestic_collector_status = QLabel("国内地点：等待进入设置")
        self.music_collector_status = QLabel("收藏音乐：点击刷新后启动")
        self.local_collector_status = QLabel("本地点：首次搜索时启动")
        for status_label in (
            self.domestic_collector_status,
            self.music_collector_status,
            self.local_collector_status,
        ):
            status_label.setObjectName("douyinCommerceCollectorStatus")
            status_label.setWordWrap(True)
            collector_status_layout.addWidget(status_label, 1)
        self.retry_collector_button = button("重试采集器", variant="secondary", compact=True)
        self.retry_collector_button.setObjectName("douyinCommerceRetryCollector")
        self.retry_collector_button.setVisible(False)
        self.retry_collector_button.clicked.connect(self._retry_last_failed_collector)
        collector_status_layout.addWidget(self.retry_collector_button)
        self.copy_collector_diagnostics_button = button(
            "复制诊断摘要", variant="secondary", compact=True
        )
        self.copy_collector_diagnostics_button.setObjectName(
            "douyinCommerceCopyCollectorDiagnostics"
        )
        self.copy_collector_diagnostics_button.clicked.connect(
            self._copy_collector_diagnostics
        )
        collector_status_layout.addWidget(self.copy_collector_diagnostics_button)
        # 采集器状态仍作为内部状态源保留，供底部提示和故障恢复逻辑使用；
        # 不再把国内地点、收藏音乐和本地点的诊断文本占用平台设置工作区。
        collector_status_bar.setVisible(False)
        layout.addWidget(collector_status_bar)

        workspace = QFrame()
        workspace.setObjectName("douyinCommercePlatformWorkspace")
        columns = QGridLayout(workspace)
        self.platform_columns = columns
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setHorizontalSpacing(14)
        columns.setVerticalSpacing(0)

        self.platform_left_column = QFrame()
        self.platform_left_column.setObjectName("douyinCommercePlatformLeftColumn")
        left_layout = QVBoxLayout(self.platform_left_column)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(14)
        self.music_stage = self._build_music_stage()
        self.declaration_stage = self._build_declaration_stage()
        self.schedule_stage = self._build_schedule_stage()
        left_layout.addWidget(self.music_stage)
        left_layout.addWidget(self.declaration_stage)
        left_layout.addWidget(self.schedule_stage)
        left_layout.addStretch(1)
        columns.addWidget(self.platform_left_column, 0, 0)

        self.platform_right_column = QFrame()
        self.platform_right_column.setObjectName("douyinCommercePlatformRightColumn")
        right_layout = QVBoxLayout(self.platform_right_column)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(14)
        self.location_stage = self._build_location_stage()
        self.batch_item_settings_stage = self._build_batch_item_settings_stage()
        right_layout.addWidget(self.location_stage)
        # 逐条地点设置填满右列，使其总高度与左侧音乐、声明和发布方式三块一致。
        right_layout.addWidget(self.batch_item_settings_stage, 1)
        columns.addWidget(self.platform_right_column, 0, 1)

        self.platform_execution_log = ExecutionLogPanel()
        columns.addWidget(self.platform_execution_log, 0, 2)

        columns.setColumnStretch(0, 3)
        columns.setColumnStretch(1, 7)
        columns.setColumnStretch(2, 0)
        columns.setColumnMinimumWidth(0, 300)
        columns.setColumnMinimumWidth(1, 300)
        self.platform_execution_log.setVisible(False)
        layout.addWidget(workspace)

        self.platform_review_dock = QFrame()
        self.platform_review_dock.setObjectName("douyinCommercePlatformReviewDock")
        review_layout = QHBoxLayout(self.platform_review_dock)
        self._configure_reference_footer(self.platform_review_dock, review_layout)
        self.platform_review_status = QLabel("完成音乐、地点和声明后可检查")
        self.platform_review_status.setObjectName("douyinCommercePlatformReviewStatus")
        self.platform_review_status.setWordWrap(True)
        review_layout.addWidget(self.platform_review_status, 1)
        self.platform_collector_progress_frame = QFrame()
        self.platform_collector_progress_frame.setObjectName(
            "douyinCommercePlatformCollectorProgress"
        )
        platform_progress_layout = QVBoxLayout(
            self.platform_collector_progress_frame
        )
        platform_progress_layout.setContentsMargins(10, 7, 10, 7)
        platform_progress_layout.setSpacing(4)
        self.platform_collector_progress_label = QLabel("")
        self.platform_collector_progress_label.setObjectName(
            "douyinCommercePlatformCollectorProgressLabel"
        )
        self.platform_collector_progress = QProgressBar()
        self.platform_collector_progress.setObjectName(
            "douyinCommercePlatformCollectorProgressBar"
        )
        self.platform_collector_progress.setRange(0, 0)
        self.platform_collector_progress.setTextVisible(False)
        self.platform_collector_progress.setFixedWidth(180)
        platform_progress_layout.addWidget(self.platform_collector_progress_label)
        platform_progress_layout.addWidget(self.platform_collector_progress)
        self.platform_collector_progress_frame.setVisible(False)
        review_layout.addWidget(self.platform_collector_progress_frame)
        review_layout.addWidget(self.platform_back_button)
        self.to_review_button = button("检查并继续", variant="primary")
        self.to_review_button.setObjectName("douyinCommerceToReview")
        self.platform_back_button.setProperty("footerAction", True)
        self.to_review_button.setProperty("footerAction", True)
        self.to_review_button.clicked.connect(self.continue_to_review)
        review_layout.addWidget(self.to_review_button)
        layout.addWidget(self.platform_review_dock)
        return self._scroll_page(body)

    def _build_batch_item_settings_stage(self) -> QFrame:
        """批量模式统一搜索地点，再逐条选择。"""

        panel = QFrame()
        panel.setProperty("subPanel", True)
        panel.setObjectName("douyinCommerceBatchItemSettings")
        panel.setProperty("douyinCommerceWorkCard", True)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(10)
        title_row = QHBoxLayout()
        self.batch_location_title_row = title_row
        title = QLabel("逐条设置地点")
        title.setObjectName("sectionTitle")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self.batch_location_commission_combo = QComboBox()
        self.batch_location_commission_combo.setObjectName(
            "douyinCommerceBatchCommissionFilter"
        )
        self.batch_location_commission_combo.addItem("全部", "all")
        self.batch_location_commission_combo.addItem("返佣", "commission")
        self.batch_location_commission_combo.addItem("无佣", "no_commission")
        self.batch_location_commission_combo.setCurrentIndex(
            self.batch_location_commission_combo.findData(
                DEFAULT_COMMISSION_FILTER
            )
        )
        self.batch_location_scope_combo = QComboBox()
        self.batch_location_scope_combo.setObjectName("douyinCommerceBatchSharedLocationScope")
        self.batch_location_scope_combo.addItem("本地", douyin_commerce_service.LOCATION_SCOPE_LOCAL)
        self.batch_location_scope_combo.addItem("国内", douyin_commerce_service.LOCATION_SCOPE_DOMESTIC)
        self.batch_location_scope_combo.setCurrentIndex(1)
        self.batch_location_keyword = QLineEdit()
        self.batch_location_keyword.setObjectName("douyinCommerceBatchSharedLocationKeyword")
        self.batch_location_keyword.setPlaceholderText("输入地点或商户名称")
        self.batch_location_search_button = button("搜索地点", variant="secondary", compact=True)
        self.batch_location_search_button.setObjectName("douyinCommerceBatchSharedSearchLocation")
        self.batch_location_search_button.clicked.connect(
            lambda: self._search_batch_locations(
                self.batch_location_scope_combo.currentData(),
                self.batch_location_keyword.text(),
            )
        )
        self.batch_location_keyword.returnPressed.connect(self.batch_location_search_button.click)
        title_row.addWidget(self.batch_location_commission_combo)
        title_row.addWidget(self.batch_location_scope_combo)
        title_row.addWidget(self.batch_location_keyword, 1)
        title_row.addWidget(self.batch_location_search_button)
        layout.addLayout(title_row)
        self.batch_item_rows = QScrollArea()
        self.batch_item_rows.setObjectName("douyinCommerceBatchItemRows")
        self.batch_item_rows.setWidgetResizable(True)
        self.batch_item_rows.setMinimumHeight(420)
        self.batch_item_rows.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.batch_item_rows.setFrameShape(QFrame.Shape.NoFrame)
        self.batch_item_rows_body = QWidget()
        self.batch_item_rows_layout = QGridLayout(self.batch_item_rows_body)
        self.batch_item_rows_layout.setContentsMargins(0, 0, 0, 0)
        self.batch_item_rows_layout.setHorizontalSpacing(0)
        self.batch_item_rows_layout.setVerticalSpacing(0)
        self.batch_item_rows_layout.setColumnStretch(0, 1)
        self.batch_item_rows.setWidget(self.batch_item_rows_body)
        layout.addWidget(self.batch_item_rows)
        self.batch_item_settings_status = QLabel("内容准备完成后读取地点候选")
        self.batch_item_settings_status.setObjectName("douyinCommerceBatchItemSettingsStatus")
        self.batch_item_settings_status.setWordWrap(True)
        self.batch_item_settings_status.setVisible(False)
        layout.addWidget(self.batch_item_settings_status)
        # 以下控件保留给既有本地草稿与测试入口；批量发布方式已移到左栏声明下方。
        self.batch_publish_mode = QComboBox(self)
        self.batch_publish_mode.addItem("立即发布", "immediate")
        self.batch_publish_mode.addItem("按间隔定时", "interval-schedule")
        self.batch_publish_mode.currentIndexChanged.connect(self._batch_schedule_mode_changed)
        self.batch_publish_mode.setVisible(False)
        self.batch_timer_enabled = QCheckBox(self)
        self.batch_timer_enabled.setVisible(False)
        self.batch_timer_enabled.toggled.connect(self._batch_timer_checkbox_changed)
        return panel

    def _batch_timer_checkbox_changed(self, checked: bool) -> None:
        target = "interval-schedule" if checked else "immediate"
        if self.batch_publish_mode.currentData() != target:
            self.batch_publish_mode.setCurrentIndex(
                self.batch_publish_mode.findData(target)
            )

    def _batch_schedule_mode_changed(self) -> None:
        enabled = self.batch_publish_mode.currentData() == "interval-schedule"
        if self.batch_timer_enabled.isChecked() != enabled:
            self.batch_timer_enabled.blockSignals(True)
            self.batch_timer_enabled.setChecked(enabled)
            self.batch_timer_enabled.blockSignals(False)
        if self.timer_enabled.isChecked() != enabled:
            self.timer_enabled.blockSignals(True)
            self.timer_enabled.setChecked(enabled)
            self.timer_enabled.blockSignals(False)
        self.batch_schedule_controls.setVisible(
            enabled and self.selected_video_count() >= 1
        )
        self._render_batch_item_rows()
        self._batch_preflight_fingerprint = ""
        self._sync_view()

    def _batch_schedule_inputs_changed(self, *_args: object) -> None:
        """排期变化后让逐条预览与预检指纹同步失效。"""

        self._batch_preflight_fingerprint = ""
        self._render_batch_item_rows()
        self._sync_view()

    def item_schedule_text(self, index: int) -> str:
        """返回第 N 条的本地排期文本；立即发布不会生成伪定时。"""

        videos = self._selected_videos()
        if not 0 <= int(index) < len(videos):
            return ""
        path = _normalized(videos[int(index)].get("storedPath"))
        override = _normalized(self._batch_schedule_overrides.get(path))
        if override:
            return override[-5:]
        if self.batch_publish_mode.currentData() != "interval-schedule":
            return "立即发布"
        start = datetime(
            self.batch_start_date.date().year(),
            self.batch_start_date.date().month(),
            self.batch_start_date.date().day(),
            self.batch_start_time.time().hour(),
            self.batch_start_time.time().minute(),
        )
        scheduled = start + timedelta(minutes=self.batch_interval_minutes.value() * int(index))
        return scheduled.strftime("%H:%M")

    def set_item_schedule_override(self, index: int, value: str) -> None:
        videos = self._selected_videos()
        if not 0 <= int(index) < len(videos):
            raise ValueError("视频条目不存在")
        raw = _normalized(value)
        try:
            datetime.strptime(raw, "%Y-%m-%d %H:%M")
        except ValueError as exc:
            raise ValueError("覆盖时间必须为 YYYY-MM-DD HH:MM") from exc
        self._batch_schedule_overrides[_normalized(videos[int(index)].get("storedPath"))] = raw
        self._batch_preflight_fingerprint = ""
        self._render_batch_item_rows()

    def _batch_schedule_override_edited(self, path: str, field: QLineEdit) -> None:
        """处理单条覆盖时间；空值表示恢复到全局间隔排期。"""

        raw = _normalized(field.text())
        if not raw:
            self._batch_schedule_overrides.pop(path, None)
            self._batch_preflight_fingerprint = ""
            self._render_batch_item_rows()
            self._sync_view()
            return
        try:
            datetime.strptime(raw, "%Y-%m-%d %H:%M")
        except ValueError:
            field.setText(_normalized(self._batch_schedule_overrides.get(path)))
            self.batch_item_settings_status.setText("覆盖时间格式应为 YYYY-MM-DD HH:MM")
            return
        self._batch_schedule_overrides[path] = raw
        self._batch_preflight_fingerprint = ""
        self._render_batch_item_rows()
        self._sync_view()

    def _current_location_presets(self) -> list[dict[str, object]]:
        account = self._selected_account() or {}
        try:
            return list_location_presets(account.get("id")) if account.get("id") else []
        except Exception:
            return []

    def _set_batch_location(self, path: str, preset: object) -> None:
        if isinstance(preset, dict):
            self._batch_locations[path] = dict(preset)
        else:
            self._batch_locations.pop(path, None)
        self._staged_location_confirmed = bool(self._batch_locations)
        self._batch_preflight_fingerprint = ""
        self._render_batch_item_rows()
        self._sync_view()

    def _batch_location_selection_changed(
        self,
        path: str,
        scope: object,
        combo: QComboBox,
        selected_index: int,
    ) -> None:
        """同步地点下拉框与本批次绑定。

        “请选择地点”不只是展示占位项；用户主动选回该项时，
        必须清除数据模型中的旧绑定，让下一次搜索能把该视频
        重新视为“未选地点”并自动填入新候选。
        """

        if selected_index <= 0:
            self._set_batch_location(path, None)
            self._set_batch_location_feedback(
                "已清除该视频的地点；再次搜索后会自动填入新地点"
            )
            return
        self._select_batch_location_candidate(path, scope, combo.currentData())

    def _batch_location_state(self) -> dict[str, object]:
        """返回本次批量共用的地点搜索状态，不把候选 DOM 写入草稿。"""

        existing = self._batch_location_searches.get(_BATCH_SHARED_LOCATION_SEARCH_KEY)
        scope = _normalized(
            (existing or {}).get("scope")
        ) or douyin_commerce_service.LOCATION_SCOPE_DOMESTIC
        commission_filter = normalize_commission_filter(
            (existing or {}).get("commissionFilter"),
            default=DEFAULT_COMMISSION_FILTER,
        )
        return {
            "scope": scope,
            "keyword": _normalized((existing or {}).get("keyword")),
            "commissionFilter": commission_filter,
            "platformResultCount": int(
                (existing or {}).get("platformResultCount") or 0
            ),
            "rawCandidates": [
                dict(item)
                for item in (existing or {}).get("rawCandidates", [])
                if isinstance(item, dict)
            ],
            "candidates": [
                dict(item)
                for item in (existing or {}).get("candidates", [])
                if isinstance(item, dict)
            ],
        }

    def _search_batch_locations(self, scope: object, keyword: object) -> None:
        """用一次设置会话读取所有视频共用的官方地点候选。"""

        if not self._setup_generation_id and not self._session_id:
            self._set_batch_location_feedback("请先完成内容准备并等待设置会话就绪")
            return
        try:
            normalized_scope = douyin_commerce_service.normalize_commerce_location_scope(scope)
        except Exception:
            self._set_batch_location_feedback("请选择地点范围：本地或国内")
            return
        normalized_keyword = _normalized(keyword)
        if not normalized_keyword:
            self._set_batch_location_feedback("请输入地点或商户名称")
            return
        commission_filter = normalize_commission_filter(
            self.batch_location_commission_combo.currentData(),
            default=DEFAULT_COMMISSION_FILTER,
        )
        self._batch_location_searches[_BATCH_SHARED_LOCATION_SEARCH_KEY] = {
            "scope": normalized_scope,
            "keyword": normalized_keyword,
            "commissionFilter": commission_filter,
            "platformResultCount": 0,
            "rawCandidates": [],
            "candidates": [],
        }
        self._set_batch_location_feedback("正在读取抖音地点候选…")
        if self._setup_generation_id:
            generation_id = self._setup_generation_id
            collector_type = (
                "domestic_location"
                if normalized_scope == douyin_commerce_service.LOCATION_SCOPE_DOMESTIC
                else "local_location"
            )
            started = self._run_collector_action(
                collector_type,
                lambda: douyin_commerce_collectors.commerce_collector_manager.search_locations(
                    generation_id,
                    normalized_keyword,
                    normalized_scope,
                    commission_filter=commission_filter,
                    include_metadata=True,
                ),
                lambda rows: self._batch_location_search_succeeded(
                    normalized_scope,
                    normalized_keyword,
                    rows,
                    commission_filter,
                ),
                action_label=(
                    "正在搜索国内地点"
                    if collector_type == "domestic_location"
                    else "正在搜索本地地点"
                ),
            )
            if not started:
                self._set_batch_location_feedback(
                    "地点搜索任务未启动，请等待当前操作结束后重试"
                )
            return
        session_id = self._session_id
        started = self._start_immediate_write(
            "batch_location_search",
            lambda: douyin_commerce_session.commerce_session_manager.search_locations(
                session_id,
                normalized_keyword,
                normalized_scope,
                commission_filter=commission_filter,
                include_metadata=True,
            ),
            lambda rows: self._batch_location_search_succeeded(
                normalized_scope,
                normalized_keyword,
                rows,
                commission_filter,
            ),
            self._batch_location_search_failed,
        )
        if not started:
            self._set_batch_location_feedback("地点搜索任务未启动，请等待当前操作结束后重试")

    def _batch_location_search_succeeded(
        self,
        scope: str,
        keyword: str,
        rows: object,
        commission_filter: object = None,
    ) -> None:
        selected_filter = normalize_commission_filter(
            commission_filter
            if commission_filter is not None
            else self.batch_location_commission_combo.currentData(),
            default=DEFAULT_COMMISSION_FILTER,
        )
        structured_result = isinstance(rows, Mapping)
        row_values = rows.get("candidates") if structured_result else rows
        public_candidates = filter_location_candidates(
            (
                [dict(item) for item in row_values if isinstance(item, Mapping)]
                if isinstance(row_values, list)
                else []
            ),
            "all",
        )
        if structured_result:
            raw_count = rows.get("platformResultCount")
            platform_result_count = (
                raw_count
                if type(raw_count) is int
                and raw_count >= len(public_candidates)
                else len(public_candidates)
            )
            candidates = public_candidates
        else:
            platform_result_count = len(public_candidates)
            candidates = filter_location_candidates(
                public_candidates,
                selected_filter,
            )
        self._batch_location_searches[_BATCH_SHARED_LOCATION_SEARCH_KEY] = {
            "scope": scope,
            "keyword": keyword,
            "commissionFilter": selected_filter,
            "platformResultCount": platform_result_count,
            "rawCandidates": [dict(item) for item in public_candidates],
            "candidates": candidates,
        }
        auto_filled, remaining = self._auto_fill_batch_location_candidates(
            scope,
            candidates,
            commission_filter=selected_filter,
        )
        filter_label = {
            "all": "全部",
            "commission": "返佣",
            "no_commission": "无佣",
        }[selected_filter]
        self._set_batch_location_feedback(
            (
                f"平台返回 {platform_result_count} 个，符合‘{filter_label}’条件 {len(candidates)} 个，自动填充 {auto_filled} 条"
                + (f"；还有 {remaining} 条待选择" if remaining else "")
            )
            if auto_filled
            else f"平台返回 {platform_result_count} 个，符合‘{filter_label}’条件 {len(candidates)} 个，请选择完整地址"
            if candidates
            else f"平台返回 {platform_result_count} 个，但没有符合‘{filter_label}’条件"
            if platform_result_count
            else "当前抖音编辑页未返回完整地点候选，请更换关键词"
        )
        self._clear_stage_error("location")
        self._render_batch_item_rows()
        self._sync_view()

    def _batch_location_search_failed(self, message: str) -> None:
        state = self._batch_location_state()
        state["candidates"] = []
        state["rawCandidates"] = []
        state["platformResultCount"] = 0
        self._batch_location_searches[_BATCH_SHARED_LOCATION_SEARCH_KEY] = state
        diagnostic = _normalized(message)
        self._set_batch_location_feedback(
            f"地点候选读取失败：{diagnostic[:180] or '请重新搜索'}"
        )
        _LOGGER.warning("抖音带货批量地点搜索失败：%s", diagnostic)
        self._platform_action_error("location", message)
        self._render_batch_item_rows()

    def _select_batch_location_candidate(
        self,
        path: str,
        scope: object,
        candidate: object,
    ) -> None:
        """选择候选后保存完整 POI 预设，并绑定到当前视频。"""

        if not self._save_batch_location_candidate(path, scope, candidate):
            self._set_batch_location_feedback("地点身份不完整，未保存")
            return
        self._staged_location_confirmed = bool(self._batch_locations)
        self._set_batch_location_feedback("已绑定地点；其他视频可继续设置")
        self._clear_stage_error("location")
        self._render_batch_item_rows()
        self._sync_view()

    def _save_batch_location_candidate(
        self,
        path: str,
        scope: object,
        candidate: object,
    ) -> bool:
        """保存一条完整 POI 并绑定视频；全程只修改本地批次配置。"""

        if not isinstance(candidate, dict):
            return False
        account = self._selected_account() or {}
        stable_candidate = {
            key: candidate.get(key)
            for key in ("poiId", "name", "address")
        }
        try:
            preset = save_location_preset(
                account.get("id"), stable_candidate, scope
            )
        except Exception as exc:
            _LOGGER.warning("抖音带货批量地点保存失败：%s", _normalized(exc))
            return False
        search_state = self._batch_location_state()
        if _normalized(search_state.get("scope")) == _normalized(scope):
            search_keyword = _normalized(search_state.get("keyword"))
            if search_keyword:
                # 原始搜索词属于本批次的恢复意图，不写入账号级地点数据库；
                # 它随视频地点快照进入草稿和正式发布载荷。
                preset["searchKeyword"] = search_keyword
        preset["commissionFilter"] = normalize_commission_filter(
            search_state.get("commissionFilter"),
            default=DEFAULT_COMMISSION_FILTER,
        )
        try:
            preset["observedCommissionType"] = (
                normalize_observed_commission_type(
                    candidate.get("commissionType"),
                    default="unknown",
                )
            )
        except ValueError:
            preset["observedCommissionType"] = "unknown"
        for key in ("productCount", "commissionProductCount"):
            value = candidate.get(key)
            preset[key] = (
                value if type(value) is int and value >= 0 else None
            )
        if "commissionLabel" in candidate:
            preset["commissionLabel"] = candidate["commissionLabel"]
        self._batch_locations[path] = dict(preset)
        self._batch_preflight_fingerprint = ""
        return True

    def _auto_fill_batch_location_candidates(
        self,
        scope: object,
        candidates: list[dict[str, object]],
        *,
        commission_filter: object = None,
    ) -> tuple[int, int]:
        """按候选顺序填充未选择地点的视频，已有设置始终保持不变。"""

        pending_paths = [
            _normalized(video.get("storedPath"))
            for video in self._selected_videos()
            if _normalized(video.get("storedPath"))
            and _normalized(video.get("storedPath")) not in self._batch_locations
        ]
        filled = 0
        selected_filter = normalize_commission_filter(
            commission_filter,
            default=DEFAULT_COMMISSION_FILTER,
        )
        for path, candidate in zip(pending_paths, candidates):
            current_state = self._batch_location_searches.get(
                _BATCH_SHARED_LOCATION_SEARCH_KEY,
                {},
            )
            current_state["commissionFilter"] = selected_filter
            if self._save_batch_location_candidate(path, scope, candidate):
                filled += 1
        return filled, max(0, len(pending_paths) - filled)

    def _batch_item_rows_state_signature(self) -> tuple[object, ...]:
        """生成地点卡片的稳定快照，避免普通状态刷新打断下拉框交互。"""

        state = self._batch_location_state()
        videos = self._selected_videos()
        video_rows = tuple(
            (
                _normalized(video.get("storedPath")),
                _normalized(video.get("filename")),
                _normalized((self._batch_locations.get(_normalized(video.get("storedPath"))) or {}).get("poiId")),
                _normalized((self._batch_locations.get(_normalized(video.get("storedPath"))) or {}).get("address")),
            )
            for video in videos
        )
        candidates = tuple(
            (
                _normalized(candidate.get("poiId")),
                _normalized(candidate.get("name")),
                _normalized(candidate.get("address")),
            )
            for candidate in state["candidates"]
            if isinstance(candidate, dict)
        )
        return (
            video_rows,
            _normalized(state["scope"]),
            _normalized(state["keyword"]),
            candidates,
        )

    def _sync_batch_location_controls(self) -> None:
        """只更新顶部搜索控件，不重建用户正展开的地点下拉框。"""

        if not hasattr(self, "batch_location_scope_combo"):
            return
        can_search = bool(self._setup_generation_id or self._session_id) and not self._busy()
        self.batch_location_commission_combo.setEnabled(can_search)
        self.batch_location_scope_combo.setEnabled(can_search)
        self.batch_location_keyword.setEnabled(can_search)
        self.batch_location_search_button.setEnabled(can_search)

    def _set_batch_location_feedback(self, message: object) -> None:
        """保留地点搜索状态，避免重绘覆盖真实的成功或失败原因。"""

        self._batch_location_feedback = _normalized(message)
        if hasattr(self, "batch_item_settings_status"):
            self.batch_item_settings_status.setText(self._batch_location_feedback)
            # 这块状态标签此前只在创建时被隐藏，后续虽然写入了“候选读取
            # 失败”等真实回读，用户却始终看不到，页面只剩空下拉框。每次
            # 搜索、选择或失败回调均应立即显示当前结果；空内容才隐藏。
            self.batch_item_settings_status.setVisible(
                bool(self._batch_location_feedback)
            )

    def _render_batch_item_rows(self) -> None:
        if not hasattr(self, "batch_item_rows"):
            return
        # 地点搜索回读会多次重绘。旧实现只从外层布局取走了嵌套列布局，
        # 列中的卡片仍保留在旧 QWidget 上，Qt 延迟销毁时会造成新旧卡片叠加。
        # 直接替换滚动区域的内容容器，确保每次仅保留当前一份地点卡片。
        old_body = self.batch_item_rows.takeWidget()
        if old_body is not None:
            old_body.deleteLater()
        self.batch_item_rows_body = QWidget()
        self.batch_item_rows_layout = QGridLayout(self.batch_item_rows_body)
        self.batch_item_rows_layout.setContentsMargins(0, 0, 0, 0)
        self.batch_item_rows_layout.setHorizontalSpacing(0)
        self.batch_item_rows_layout.setVerticalSpacing(0)
        self.batch_item_rows_layout.setColumnStretch(0, 1)
        self.batch_item_rows.setWidget(self.batch_item_rows_body)
        videos = self._selected_videos()
        if not videos:
            if not self._batch_location_feedback:
                self._set_batch_location_feedback("返回内容准备选择视频")
            self._batch_item_rows_signature = self._batch_item_rows_state_signature()
            return
        state = self._batch_location_state()
        self._sync_batch_location_controls()
        scope_index = self.batch_location_scope_combo.findData(state["scope"])
        if scope_index >= 0 and self.batch_location_scope_combo.currentIndex() != scope_index:
            self.batch_location_scope_combo.blockSignals(True)
            self.batch_location_scope_combo.setCurrentIndex(scope_index)
            self.batch_location_scope_combo.blockSignals(False)
        if self.batch_location_keyword.text() != str(state["keyword"]):
            self.batch_location_keyword.blockSignals(True)
            self.batch_location_keyword.setText(str(state["keyword"]))
            self.batch_location_keyword.blockSignals(False)
        missing_location = 0
        for index, video in enumerate(videos):
            path = _normalized(video.get("storedPath"))
            row = QFrame()
            row.setObjectName("douyinCommerceBatchItemRow")
            row.setFixedHeight(35)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(8, 1, 8, 1)
            row_layout.setSpacing(8)
            filename = _normalized(video.get("filename"))
            compact_name = filename if len(filename) <= 20 else f"{filename[:19]}…"
            name = QLabel(f"{index + 1}. {compact_name}")
            name.setToolTip(_normalized(video.get("filename")))
            name.setMinimumWidth(0)
            name.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            row_layout.addWidget(name, 3)
            current = self._batch_locations.get(path) or {}
            candidate_combo = QComboBox()
            candidate_combo.setObjectName("douyinCommerceBatchLocationCandidates")
            candidate_combo.setFixedHeight(30)
            candidate_combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            candidate_combo.addItem("请选择地点", None)
            current_index = 0
            for candidate in state["candidates"]:
                suffix = {
                    "commission": "【返佣】",
                    "no_commission": "【无佣】",
                    "unknown": "【待确认】",
                }.get(_normalized(candidate.get("commissionType")), "【待确认】")
                label = (
                    f"{candidate.get('name') or ''} · "
                    f"{candidate.get('address') or ''}{suffix}"
                )
                candidate_combo.addItem(label, dict(candidate))
                candidate_combo.setItemData(
                    candidate_combo.count() - 1,
                    label,
                    Qt.ItemDataRole.ToolTipRole,
                )
                if _normalized(candidate.get("poiId")) == _normalized(current.get("poiId")):
                    current_index = candidate_combo.count() - 1
            # 顶部重新搜索其他关键词时，也要保留每条视频已经选中的地点并直接
            # 显示在下拉框中；不再额外重复展示一块地址文本。
            if current and current_index == 0:
                suffix = {
                    "commission": "【返佣】",
                    "no_commission": "【无佣】",
                    "unknown": "【待确认】",
                }.get(
                    _normalized(current.get("observedCommissionType")),
                    "【待确认】",
                )
                label = (
                    f"{current.get('name') or ''} · "
                    f"{current.get('address') or ''}{suffix}"
                )
                candidate_combo.addItem(label, dict(current))
                candidate_combo.setItemData(
                    candidate_combo.count() - 1,
                    label,
                    Qt.ItemDataRole.ToolTipRole,
                )
                current_index = candidate_combo.count() - 1
            candidate_combo.setCurrentIndex(current_index)
            candidate_combo.currentIndexChanged.connect(
                lambda selected_index, item_path=path, combo=candidate_combo: self._batch_location_selection_changed(
                    item_path, state["scope"], combo, selected_index
                )
            )
            # 选择地点只更新本地批次配置，不依赖搜索任务是否刚完成；否则
            # 成功回调与线程清理的短暂间隙会让用户看到候选却无法选择。
            candidate_combo.setEnabled(bool(state["candidates"]) or bool(current))
            row_layout.addWidget(candidate_combo, 7)
            if not current:
                missing_location += 1
            self.batch_item_rows_layout.addWidget(row, index, 0)
        self.batch_item_rows_layout.setRowStretch(len(videos), 1)
        if not self._batch_location_feedback:
            self._set_batch_location_feedback(
                "在顶部搜索后，为每条视频选择一个地点"
                if missing_location
                else "地点已填写；预检时会逐条重新搜索并精确回读"
            )
        self._batch_item_rows_signature = self._batch_item_rows_state_signature()

    @staticmethod
    def _checklist_value(label_text: str) -> QLabel:
        value = QLabel("待完成")
        value.setObjectName("douyinCommerceSessionChecklistValue")
        value.setAccessibleName(f"{label_text}状态")
        value.setWordWrap(True)
        value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return value

    def _build_blocked_stage(self) -> QFrame:
        panel, panel_layout = self._section(
            "等待上传会话",
            "请返回内容准备，核对账号、视频和文案后手动上传；本页不会自动访问抖音。",
        )
        panel.setObjectName("douyinCommerceBlockedStage")
        self.blocked_stage_message = QLabel("当前没有可继续配置的抖音编辑会话")
        self.blocked_stage_message.setObjectName("douyinCommerceInlineNotice")
        self.blocked_stage_message.setWordWrap(True)
        panel_layout.addWidget(self.blocked_stage_message)
        return panel

    def _build_music_stage(self) -> QFrame:
        panel, panel_layout = self._section(
            "选择收藏音乐",
            "",
        )
        panel.setObjectName("douyinCommerceMusicStage")
        panel.setProperty("douyinCommerceWorkCard", True)
        self.music_panel = panel
        music_actions = QHBoxLayout()
        self.music_combo = _LazyMusicComboBox()
        self.music_combo.setObjectName("douyinCommerceMusicCombo")
        self.music_combo.addItem("选择收藏音乐", None)
        self.music_combo.setAccessibleName("收藏音乐")
        self.music_combo.picker_requested.connect(self._load_favorite_music_candidates)
        self.music_combo.candidate_list_requested.connect(self._toggle_music_candidate_list)
        self.music_combo.activated.connect(self._music_combo_activated)
        music_actions.addWidget(self.music_combo, 1)
        self.music_refresh_button = button("刷新", variant="secondary", compact=True)
        self.music_refresh_button.setObjectName("douyinCommerceRefreshFavoriteMusic")
        self.music_refresh_button.setAccessibleName("刷新收藏音乐")
        self.music_refresh_button.clicked.connect(self._refresh_favorite_music_candidates)
        music_actions.addWidget(self.music_refresh_button)
        panel_layout.addLayout(music_actions)
        self.music_candidate_list = QListWidget()
        self.music_candidate_list.setObjectName("douyinCommerceMusicCandidates")
        self.music_candidate_list.setMinimumHeight(0)
        self.music_candidate_list.setMaximumHeight(184)
        self.music_candidate_list.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.music_candidate_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.music_candidate_list.setVisible(False)
        self.music_candidate_list.itemClicked.connect(self._music_candidate_clicked)
        panel_layout.addWidget(self.music_candidate_list)
        self.music_status = QLabel("上传后直接选择")
        self.music_status.setObjectName("douyinCommerceInlineNotice")
        self.music_status.setWordWrap(True)
        self.music_status.setVisible(False)
        panel_layout.addWidget(self.music_status)
        self.music_card = self._selection_card("尚未选择收藏音乐")
        self.music_card.setObjectName("douyinCommerceMusicCard")
        self.music_card.setVisible(False)
        panel_layout.addWidget(self.music_card)
        panel_layout.addWidget(self._stage_error_label("music"))
        return panel

    def _build_location_stage(self) -> QFrame:
        panel, panel_layout = self._section(
            "选择发布定位",
            "搜索后确认完整地址。",
        )
        panel.setObjectName("douyinCommerceLocationStage")
        panel.setProperty("douyinCommerceWorkCard", True)
        self.location_panel = panel
        self.location_view_stack = QStackedWidget()
        self.location_view_stack.setObjectName("douyinCommerceLocationViewStack")
        self.location_search_view = self._build_location_search_view()
        self.location_candidate_view = self._build_location_candidate_view()
        self.location_applied_view = self._build_location_applied_view()
        for view in (
            self.location_search_view,
            self.location_candidate_view,
            self.location_applied_view,
        ):
            self.location_view_stack.addWidget(view)
        panel_layout.addWidget(self.location_view_stack)
        panel_layout.addWidget(self._stage_error_label("location"))
        return panel

    def _build_location_search_view(self) -> QWidget:
        view = QWidget()
        layout = QVBoxLayout(view)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        search_row = QHBoxLayout()
        self.location_scope_combo = QComboBox()
        self.location_scope_combo.setObjectName("douyinCommerceLocationScope")
        self.location_scope_combo.addItem("选择范围：本地或国内", "")
        self.location_scope_combo.addItem(
            douyin_commerce_service.location_scope_label(
                douyin_commerce_service.LOCATION_SCOPE_LOCAL
            ),
            douyin_commerce_service.LOCATION_SCOPE_LOCAL,
        )
        self.location_scope_combo.addItem(
            douyin_commerce_service.location_scope_label(
                douyin_commerce_service.LOCATION_SCOPE_DOMESTIC
            ),
            douyin_commerce_service.LOCATION_SCOPE_DOMESTIC,
        )
        self.location_scope_combo.setCurrentIndex(2)
        self.location_scope_combo.currentIndexChanged.connect(self._location_scope_changed)
        self.location_keyword = QLineEdit()
        self.location_keyword.setObjectName("douyinCommerceLocationKeyword")
        self.location_keyword.setPlaceholderText("输入地点或商户名称")
        self.location_keyword.returnPressed.connect(self.search_locations)
        self.location_keyword.textEdited.connect(self._location_keyword_edited)
        self.location_search_button = button("搜索发布定位", variant="primary", compact=True)
        self.location_search_button.setObjectName("douyinCommerceSearchLocation")
        self.location_search_button.clicked.connect(self.search_locations)
        search_row.addWidget(self.location_scope_combo)
        search_row.addWidget(self.location_keyword, 1)
        search_row.addWidget(self.location_search_button)
        layout.addLayout(search_row)
        self.location_status = QLabel("上传后可搜索发布定位（默认范围：国内）")
        self.location_status.setObjectName("douyinCommerceLocationStatus")
        self.location_status.setWordWrap(True)
        layout.addWidget(self.location_status)
        self.location_result_list = QListWidget()
        self.location_result_list.setObjectName("douyinCommerceLocationResults")
        self.location_result_list.setMinimumHeight(0)
        self.location_result_list.setMaximumHeight(260)
        self.location_result_list.setVisible(False)
        self.location_result_list.setWordWrap(True)
        self.location_result_list.setUniformItemSizes(False)
        self.location_result_list.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.location_result_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.location_result_list.itemSelectionChanged.connect(self._location_selected)
        layout.addWidget(self.location_result_list)
        return view

    def _build_location_candidate_view(self) -> QWidget:
        view = QWidget()
        layout = QVBoxLayout(view)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.location_candidate_card = self._selection_card("请选择一个平台发布定位候选")
        self.location_candidate_card.setObjectName("douyinCommerceLocationCandidateCard")
        layout.addWidget(self.location_candidate_card)
        layout.addStretch(1)
        return view

    def _build_location_applied_view(self) -> QWidget:
        view = QWidget()
        layout = QVBoxLayout(view)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.location_applied_card = self._selection_card("尚未选择发布定位")
        self.location_applied_card.setObjectName("douyinCommerceLocationConfirmedCard")
        self.change_location_button = button("更换定位", variant="secondary", compact=True)
        self.change_location_button.setObjectName("douyinCommerceChangeLocation")
        self.change_location_button.clicked.connect(self.change_location_selection)
        layout.addWidget(self.location_applied_card)
        layout.addWidget(self.change_location_button)
        layout.addStretch(1)
        return view

    def _build_declaration_stage(self) -> QFrame:
        panel, panel_layout = self._section(
            "作品内容声明",
            "",
        )
        panel.setObjectName("douyinCommerceDeclarationStage")
        panel.setProperty("douyinCommerceWorkCard", True)
        self.declaration_panel = panel
        self.declaration_group = QButtonGroup(panel)
        self.declaration_buttons: dict[str, QRadioButton] = {}
        for declaration in douyin_commerce_service.CONTENT_DECLARATION_OPTIONS:
            option = QRadioButton(declaration)
            option.setObjectName("douyinCommerceContentDeclarationOption")
            option.setAccessibleName(f"作品内容声明：{declaration}")
            if declaration in self._DISABLED_CONTENT_DECLARATIONS:
                option.setToolTip("当前发布流程不支持选择转载信息声明")
                option.setEnabled(False)
            option.toggled.connect(
                lambda checked, value=declaration: self._declaration_toggled(value, checked)
            )
            self.declaration_group.addButton(option)
            self.declaration_buttons[declaration] = option
            panel_layout.addWidget(option)
        self._set_selected_declaration(self._DEFAULT_CONTENT_DECLARATION)
        self.declaration_status = QLabel("上传后自动写入默认声明")
        self.declaration_status.setProperty("role", "caption")
        self.declaration_status.setWordWrap(True)
        self.declaration_status.setVisible(False)
        panel_layout.addWidget(self.declaration_status)
        # 单选项本身已展示并回读当前声明；不再重复渲染长摘要卡，避免占用设置区。
        self.declaration_card = self._selection_card("尚未选择作品内容声明")
        self.declaration_card.setObjectName("douyinCommerceDeclarationCard")
        self.declaration_card.setVisible(False)
        panel_layout.addWidget(self._stage_error_label("declaration"))
        return panel

    def _build_schedule_stage(self) -> QFrame:
        panel, panel_layout = self._section(
            "发布方式",
            "默认立即发表；定时使用北京时间。",
        )
        panel.setObjectName("douyinCommerceScheduleStage")
        panel.setProperty("douyinCommerceWorkCard", True)
        self.schedule_panel = panel
        self.publish_mode_hint = QLabel("当前为立即发表；仍需经过预检和最终确认，系统不会自动提交。")
        self.publish_mode_hint.setObjectName("douyinCommercePublishModeHint")
        self.publish_mode_hint.setWordWrap(True)
        panel_layout.addWidget(self.publish_mode_hint)
        self.timer_enabled = QCheckBox("开启定时发表")
        self.timer_enabled.setChecked(False)
        self.timer_enabled.toggled.connect(self._timer_enabled_changed)
        panel_layout.addWidget(self.timer_enabled)
        schedule_row = QHBoxLayout()
        schedule_row.setSpacing(8)
        self.batch_schedule_row = schedule_row
        self.schedule_date = QDateEdit()
        self.schedule_date.setObjectName("douyinCommerceScheduleDate")
        self.schedule_date.setCalendarPopup(True)
        default_schedule = self._default_schedule_datetime()
        self.schedule_date.setMinimumDate(
            QDate.currentDate()
        )
        self.schedule_date.setDate(
            QDate(
                default_schedule.year,
                default_schedule.month,
                default_schedule.day,
            )
        )
        self.schedule_date.dateChanged.connect(self._schedule_changed)
        self.schedule_time = QTimeEdit()
        self.schedule_time.setObjectName("douyinCommerceScheduleTime")
        self.schedule_time.setDisplayFormat("HH:mm")
        self.schedule_time.setTime(QTime(16, 0))
        self.schedule_time.timeChanged.connect(self._schedule_changed)
        schedule_row.addWidget(self.schedule_date)
        schedule_row.addWidget(self.schedule_time)
        self.batch_schedule_controls = QWidget()
        self.batch_schedule_controls.setObjectName("douyinCommerceBatchIntervalControls")
        interval_layout = QHBoxLayout(self.batch_schedule_controls)
        interval_layout.setContentsMargins(0, 0, 0, 0)
        interval_layout.setSpacing(8)
        interval_layout.addWidget(QLabel("间隔"))
        self.batch_interval_minutes = QSpinBox()
        self.batch_interval_minutes.setRange(1, 1440)
        self.batch_interval_minutes.setValue(30)
        self.batch_interval_minutes.setSuffix(" 分钟")
        self.batch_interval_minutes.valueChanged.connect(self._batch_schedule_inputs_changed)
        interval_layout.addWidget(self.batch_interval_minutes)
        schedule_row.addWidget(self.batch_schedule_controls)
        schedule_row.addStretch(1)
        panel_layout.addLayout(schedule_row)
        # 批量与单条共用同一组日期/时间，避免用户在两个区域重复填写。
        self.batch_start_date = self.schedule_date
        self.batch_start_time = self.schedule_time
        panel_layout.addWidget(self._stage_error_label("schedule"))
        return panel

    def _build_review_page(self) -> QWidget:
        body = QWidget()
        body.setObjectName("douyinCommerceStepBody")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 14, 0)
        layout.setSpacing(16)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        review_workspace = QFrame()
        review_workspace.setObjectName("douyinCommerceReviewWorkspace")
        review_columns = QGridLayout(review_workspace)
        self.review_columns = review_columns
        review_columns.setContentsMargins(0, 0, 0, 0)
        review_columns.setHorizontalSpacing(16)
        review_columns.setVerticalSpacing(0)

        panel, panel_layout = self._section(
            "检查并提交",
            "",
        )
        panel.setObjectName("douyinCommerceReviewPanel")
        panel.setProperty("douyinCommerceWorkCard", True)
        self.review_panel = panel
        self.summary_grid = QGridLayout()
        self.summary_grid.setHorizontalSpacing(10)
        self.summary_grid.setVerticalSpacing(10)
        self.summary_values: dict[str, QLabel] = {}
        summary_sections = (
            (
                "内容确认",
                (("账号", "account"), ("视频", "video"), ("标题", "title"), ("作品文案", "description")),
            ),
            (
                "发布设置",
                (("用户所选音乐", "music"), ("发布位置", "location"), ("作品内容声明", "declaration"), ("发布方式", "schedule")),
            ),
        )
        grid_row = 0
        for section_text, fields in summary_sections:
            section_title = QLabel(section_text)
            section_title.setObjectName("douyinCommerceReviewSummarySection")
            self.summary_grid.addWidget(section_title, grid_row, 0, 1, 2)
            grid_row += 1
            for field_index, (label_text, key) in enumerate(fields):
                item = QFrame()
                item.setObjectName("douyinCommerceReviewSummaryItem")
                item_layout = QVBoxLayout(item)
                item_layout.setContentsMargins(12, 10, 12, 10)
                item_layout.setSpacing(5)
                label = QLabel(label_text)
                label.setObjectName("douyinCommerceReviewSummaryLabel")
                value = QLabel("待完成")
                value.setObjectName("douyinCommerceReviewSummaryValue")
                value.setWordWrap(True)
                value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                item_layout.addWidget(label)
                item_layout.addWidget(value)
                self.summary_grid.addWidget(item, grid_row + field_index // 2, field_index % 2)
                self.summary_values[key] = value
            grid_row += (len(fields) + 1) // 2
        self.summary_grid.setColumnStretch(0, 1)
        self.summary_grid.setColumnStretch(1, 1)
        panel_layout.addLayout(self.summary_grid)

        self.review_submission_panel, submission_layout = self._section(
            "逐条发布信息",
            "",
        )
        self.review_submission_panel.setObjectName("douyinCommerceReviewSubmissionPanel")
        self.review_submission_panel.setProperty("douyinCommerceWorkCard", True)
        title_item = submission_layout.takeAt(0)
        self.review_submission_title = title_item.widget() if title_item else QLabel()
        self.review_submission_header = QFrame()
        self.review_submission_header.setObjectName("douyinCommerceReviewSubmissionHeader")
        header_layout = QHBoxLayout(self.review_submission_header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)
        header_layout.addWidget(self.review_submission_title)
        header_layout.addStretch(1)
        # 保留旧属性供既有状态同步使用，但不再创建或显示重复的说明文本。
        self.review_submission_helper = QLabel()
        self.review_submission_helper.setVisible(False)
        self.copy_batch_publish_info_button = button(
            "复制发布信息", variant="secondary", compact=True
        )
        self.copy_batch_publish_info_button.setObjectName(
            "douyinCommerceCopyBatchPublishInfo"
        )
        self.copy_batch_publish_info_button.clicked.connect(
            self.copy_batch_publish_information
        )
        header_layout.addWidget(self.copy_batch_publish_info_button)
        submission_layout.insertWidget(0, self.review_submission_header)
        self.batch_review_rows = QScrollArea()
        self.batch_review_rows.setObjectName("douyinCommerceBatchReviewRows")
        self.batch_review_rows.setWidgetResizable(True)
        self.batch_review_rows.setFrameShape(QFrame.Shape.NoFrame)
        self.batch_review_rows.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.batch_review_rows_body = QWidget()
        self.batch_review_rows_layout = QVBoxLayout(self.batch_review_rows_body)
        self.batch_review_rows_layout.setContentsMargins(0, 0, 0, 0)
        self.batch_review_rows_layout.setSpacing(8)
        self.batch_review_rows.setWidget(self.batch_review_rows_body)
        self.batch_review_rows.setVisible(False)
        self.batch_review_rows.setMinimumHeight(420)
        submission_layout.addWidget(self.batch_review_rows, 1)
        self.validation_label = QLabel("确认信息后提交发布。")
        self.validation_label.setObjectName("douyinCommerceReviewValidation")
        self.validation_label.setWordWrap(True)
        self.preflight_button = button("执行发布前检查", variant="primary")
        self.preflight_button.setObjectName("douyinCommercePreflight")
        self.preflight_button.clicked.connect(self.start_preflight)
        # 发布前检查不再是用户流程的一步；保留对象仅兼容旧会话，界面始终隐藏。
        self.preflight_button.setVisible(False)
        self.submit_button = button("确认立即发表", variant="secondary")
        self.submit_button.setObjectName("douyinCommerceSubmit")
        self.submit_button.clicked.connect(self.open_submit_confirmation)
        self.pause_batch_button = button("暂停发布", variant="secondary")
        self.pause_batch_button.setObjectName("douyinCommercePauseBatchPublish")
        self.pause_batch_button.setVisible(False)
        self.pause_batch_button.clicked.connect(self.pause_batch_publish)

        review_columns.addWidget(panel, 0, 0, Qt.AlignmentFlag.AlignTop)
        review_columns.addWidget(self.review_submission_panel, 0, 1)
        self.review_execution_log = ExecutionLogPanel()
        review_columns.addWidget(self.review_execution_log, 0, 2)
        review_columns.setColumnStretch(0, 25)
        review_columns.setColumnStretch(1, 50)
        review_columns.setColumnStretch(2, 25)
        review_columns.setColumnMinimumWidth(0, 270)
        review_columns.setColumnMinimumWidth(2, 300)
        self.review_execution_log.setMinimumWidth(300)
        layout.addWidget(review_workspace)

        self.review_action_dock = QFrame()
        self.review_action_dock.setObjectName("douyinCommerceReferenceFooter")
        navigation = QHBoxLayout(self.review_action_dock)
        self._configure_reference_footer(self.review_action_dock, navigation)
        navigation.addWidget(self.validation_label, 1)
        self.background_mode_checkbox = QCheckBox("后台运行")
        self.background_mode_checkbox.setObjectName(
            "douyinCommerceBackgroundMode"
        )
        self.background_mode_checkbox.setChecked(True)
        self.background_mode_checkbox.setToolTip(
            "默认使用后台浏览器发布；取消勾选后会显示正式发布浏览器，"
            "用于观察地点、声明或平台风控失败。"
        )
        navigation.addWidget(self.background_mode_checkbox)
        self.review_back_button = button("返回平台设置", variant="secondary")
        self.review_back_button.setProperty("footerAction", True)
        self.review_back_button.clicked.connect(self.return_from_review)
        self.preflight_button.setProperty("footerAction", True)
        self.submit_button.setProperty("footerAction", True)
        self.pause_batch_button.setProperty("footerAction", True)
        navigation.addWidget(self.review_back_button)
        navigation.addWidget(self.preflight_button)
        navigation.addWidget(self.submit_button)
        navigation.addWidget(self.pause_batch_button)
        layout.addWidget(self.review_action_dock)
        return self._scroll_page(body)

    @staticmethod
    def _selection_card(initial_text: str) -> QLabel:
        card = QLabel(initial_text)
        card.setProperty("role", "caption")
        card.setProperty("douyinCommerceSelectionCard", True)
        card.setWordWrap(True)
        card.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return card

    def refresh(self) -> None:
        """刷新本地账号和素材选择，不自动检测或打开平台浏览器。"""

        previous_account = self._account_key(self._selected_account())
        previous_video = self._video_key(self._selected_video())
        self.account_combo.blockSignals(True)
        self.account_combo.clear()
        self.account_combo.addItem("请选择已登录的抖音账号", None)
        for account in account_service.list_accounts():
            if int(account.get("type") or 0) != 3 or int(account.get("status") or 0) != 1:
                continue
            account_name, subject = _account_identity(account)
            if not account_name:
                continue
            display = (
                account_name
                if not subject or subject == account_name
                else f"{account_name} · {subject}"
            )
            self.account_combo.addItem(
                QIcon(_account_avatar_pixmap(account, 26)), display, dict(account)
            )
        self._restore_combo_data(self.account_combo, previous_account, self._account_key)
        self.account_combo.blockSignals(False)

        self.video_combo.blockSignals(True)
        self.video_combo.clear()
        self.video_combo.addItem("请选择一个视频素材", None)
        for media in media_service.list_media():
            if str(media.get("typeText") or "") != "视频":
                continue
            stored_path = str(media.get("storedPath") or "")
            if not stored_path or not Path(stored_path).is_file():
                continue
            self.video_combo.addItem(str(media.get("filename") or Path(stored_path).name), dict(media))
        self._restore_combo_data(self.video_combo, previous_video, self._video_key)
        self.video_combo.blockSignals(False)
        self._refresh_batch_video_list()
        self._refresh_saved_content_status()
        self._refresh_batch_saved_content_status()
        self._load_tag_history()
        self._render_selected_tags()
        self._sync_content_cards()
        self._sync_view()

    @staticmethod
    def _restore_combo_data(combo: QComboBox, key: str, key_fn) -> None:
        if not key:
            return
        for index in range(combo.count()):
            if key_fn(combo.itemData(index)) == key:
                combo.setCurrentIndex(index)
                return

    @staticmethod
    def _account_key(account: object) -> str:
        return _normalized((account or {}).get("filePath")) if isinstance(account, dict) else ""

    @staticmethod
    def _video_key(media: object) -> str:
        return _normalized((media or {}).get("storedPath")) if isinstance(media, dict) else ""

    def _refresh_batch_video_list(self) -> None:
        """用多选缩略图列表呈现本机视频；刷新绝不访问平台。"""

        previous_paths = {
            _normalized((self.video_combo.itemData(index) or {}).get("storedPath"))
            for index in self._selected_video_indexes
            if 0 < index < self.video_combo.count()
            and isinstance(self.video_combo.itemData(index), dict)
        }
        self.batch_video_list.blockSignals(True)
        self.batch_video_list.clear()
        self._selected_video_indexes = []
        for index in range(1, self.video_combo.count()):
            media = self.video_combo.itemData(index)
            if not isinstance(media, dict):
                continue
            item = QListWidgetItem(self._video_picker_icon(media), self._video_picker_title(self.video_combo.itemText(index)))
            item.setToolTip(self._video_display(media, media_service.video_display_metadata(media)))
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked = _normalized(media.get("storedPath")) in previous_paths
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            item.setSizeHint(QSize(0, _VIDEO_PICKER_ROW_HEIGHT))
            self.batch_video_list.addItem(item)
            if checked:
                self._selected_video_indexes.append(index)
        self.batch_video_list.blockSignals(False)
        self._sync_batch_video_status()

    def _batch_video_item_changed(self, item: QListWidgetItem) -> None:
        index = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(index, int):
            return
        selected = [
            int(self.batch_video_list.item(row).data(Qt.ItemDataRole.UserRole))
            for row in range(self.batch_video_list.count())
            if self.batch_video_list.item(row).checkState() == Qt.CheckState.Checked
        ]
        if len(selected) > 20:
            item.setCheckState(Qt.CheckState.Unchecked)
            QMessageBox.warning(self, "选择视频", "一次最多选择 20 条视频。")
            return
        self._selected_video_indexes = selected
        if selected:
            self.video_combo.blockSignals(True)
            self.video_combo.setCurrentIndex(selected[0])
            self.video_combo.blockSignals(False)
        self._sync_batch_video_status()
        self._content_changed()

    def select_video_indexes(self, indexes: list[int]) -> None:
        """供界面测试和恢复草稿使用的受限多视频选择入口。"""

        unique = []
        for index in indexes:
            if isinstance(index, int) and 1 <= index < self.video_combo.count() and index not in unique:
                unique.append(index)
        if not 1 <= len(unique) <= 20:
            raise ValueError("请选择 1 至 20 条视频")
        self.batch_video_list.blockSignals(True)
        for row in range(self.batch_video_list.count()):
            item = self.batch_video_list.item(row)
            item_index = item.data(Qt.ItemDataRole.UserRole)
            item.setCheckState(
                Qt.CheckState.Checked if item_index in unique else Qt.CheckState.Unchecked
            )
        self.batch_video_list.blockSignals(False)
        self._selected_video_indexes = unique
        self.video_combo.blockSignals(True)
        self.video_combo.setCurrentIndex(unique[0])
        self.video_combo.blockSignals(False)
        self._sync_batch_video_status()
        self._content_changed()

    def select_all_batch_videos(self) -> None:
        """选择当前素材列表中最多二十条视频，不访问平台。"""

        if self._busy():
            QMessageBox.warning(self, "全选视频", "当前正在处理，请等待操作完成后再修改视频。")
            return
        indexes = list(range(1, min(self.video_combo.count(), 21)))
        if not indexes:
            QMessageBox.information(self, "全选视频", "素材管理中暂未找到可选视频。")
            return
        self.select_video_indexes(indexes)

    def clear_batch_video_selection(self) -> None:
        """取消所有视频勾选，仅清空本地批次选择。"""

        if self._busy():
            QMessageBox.warning(self, "取消全选", "当前正在处理，请等待操作完成后再修改视频。")
            return
        self.batch_video_list.blockSignals(True)
        for row in range(self.batch_video_list.count()):
            self.batch_video_list.item(row).setCheckState(Qt.CheckState.Unchecked)
        self.batch_video_list.blockSignals(False)
        self._selected_video_indexes = []
        self.video_combo.blockSignals(True)
        self.video_combo.setCurrentIndex(0)
        self.video_combo.blockSignals(False)
        self._sync_batch_video_status()
        self._content_changed()

    def selected_video_count(self) -> int:
        return len(self._selected_video_indexes)

    def _selected_videos(self) -> list[dict]:
        result: list[dict] = []
        for index in self._selected_video_indexes:
            value = self.video_combo.itemData(index)
            if isinstance(value, dict):
                result.append(dict(value))
        return result

    def _sync_batch_video_status(self) -> None:
        count = self.selected_video_count()
        if count:
            self.batch_video_status.setText(f"已选择 {count} 条视频")
        else:
            self.batch_video_status.setText("选择 1 至 20 条视频")

    def _selected_account(self) -> dict | None:
        data = self.account_combo.currentData()
        return dict(data) if isinstance(data, dict) else None

    def _selected_video(self) -> dict | None:
        data = self.video_combo.currentData()
        return dict(data) if isinstance(data, dict) else None

    def _selected_location(self) -> dict[str, str] | None:
        if self._selected_location_data is not None:
            return dict(self._selected_location_data)
        selected = self.location_result_list.selectedItems()
        if not selected:
            return None
        data = selected[0].data(Qt.ItemDataRole.UserRole)
        return dict(data) if isinstance(data, dict) else None

    def _selected_location_scope(self) -> str:
        return str(self.location_scope_combo.currentData() or "")

    def _selected_declaration(self) -> str:
        for declaration, option in self.declaration_buttons.items():
            if option.isChecked():
                return declaration
        return ""

    def _selected_music_candidate(self) -> dict[str, str] | None:
        data = self.music_combo.currentData()
        return dict(data) if isinstance(data, dict) else None

    def _set_selected_declaration(self, declaration: str) -> None:
        """仅同步客户端单选状态；平台写入始终经即时任务与回读完成。"""

        option = self.declaration_buttons.get(declaration)
        if option is None:
            return
        previous = self._suppress_declaration_signal
        self._suppress_declaration_signal = True
        try:
            option.setChecked(True)
        finally:
            self._suppress_declaration_signal = previous

    def _restore_music_combo(self, music: dict[str, str] | None) -> None:
        """回显当前音乐；批量模式的本地暂选也必须显示在选择框中。"""

        target_id = _normalized((music or {}).get("musicId"))
        self.music_combo.blockSignals(True)
        try:
            index = 0
            if target_id:
                for candidate_index in range(1, self.music_combo.count()):
                    candidate = self.music_combo.itemData(candidate_index)
                    if isinstance(candidate, dict) and _normalized(candidate.get("musicId")) == target_id:
                        index = candidate_index
                        break
                if index == 0 and isinstance(music, dict):
                    title = _normalized(music.get("title")) or "未命名音乐"
                    creator = _normalized(music.get("creator")) or "未知作者"
                    duration = _normalized(music.get("duration"))
                    suffix = f" · {duration}" if duration else ""
                    self.music_combo.addItem(
                        f"{title} · {creator}{suffix}",
                        dict(music),
                    )
                    index = self.music_combo.count() - 1
            self.music_combo.setCurrentIndex(index)
        finally:
            self.music_combo.blockSignals(False)

    def _clear_music_candidates(self) -> None:
        """清空瞬态收藏列表，不让旧会话的候选流入新上传会话。"""

        self._music_candidates = []
        self._music_candidate_source = ""
        self._pending_music = None
        self._open_music_picker_after_load = False
        self.music_candidate_list.clear()
        self.music_candidate_list.setVisible(False)
        self.music_combo.blockSignals(True)
        try:
            self.music_combo.clear()
            self.music_combo.addItem("选择收藏音乐", None)
            self.music_combo.setCurrentIndex(0)
        finally:
            self.music_combo.blockSignals(False)

    def _discard_music_candidates_for_reload(self) -> None:
        """丢弃仅对当次弹窗有效的候选，并保留已确认音乐的展示。"""

        self._music_candidates = []
        self._music_candidate_source = ""
        self._pending_music = None
        self._open_music_picker_after_load = False
        self.music_candidate_list.clear()
        self.music_candidate_list.setVisible(False)
        self.music_combo.blockSignals(True)
        try:
            self.music_combo.clear()
            if self._selected_music:
                self.music_combo.addItem(
                    f"当前音乐：{self._music_display(self._selected_music)}（点击更换）",
                    None,
                )
            else:
                self.music_combo.addItem("选择收藏音乐", None)
            self.music_combo.setCurrentIndex(0)
        finally:
            self.music_combo.blockSignals(False)

    @staticmethod
    def _parse_tag_text(value: object) -> list[str]:
        """解析用户输入的标签，保留顺序并去重。"""

        tags: list[str] = []
        for fragment in re.split(r"[,，、;；\s]+", str(value or "")):
            tag = _normalized(fragment).lstrip("#").strip()
            if tag and tag not in tags:
                tags.append(tag)
        return tags

    @staticmethod
    def _clear_layout(layout: QHBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _render_selected_tags(self) -> None:
        """把已添加的标签渲染为可删除的紧凑芯片。"""

        self._clear_layout(self.selected_tags_layout)
        if not self._tag_values:
            placeholder = QLabel("尚未添加标签")
            placeholder.setProperty("role", "caption")
            self.selected_tags_layout.addWidget(placeholder)
        else:
            for tag in self._tag_values:
                chip = button(f"#{tag} ×", variant="secondary", compact=True)
                chip.setObjectName("douyinCommerceTagChip")
                chip.setToolTip(f"移除 #{tag}")
                chip.clicked.connect(
                    lambda _checked=False, value=tag: self.remove_tag(value)
                )
                self.selected_tags_layout.addWidget(chip)
        self.selected_tags_layout.addStretch(1)

    def _render_tag_history(self) -> None:
        """渲染本机历史标签；点击仅回填本页，不访问平台。"""

        self._clear_layout(self.tag_history_layout)
        if not self._tag_history:
            placeholder = QLabel("保存内容后会在这里保留常用标签")
            placeholder.setProperty("role", "caption")
            self.tag_history_layout.addWidget(placeholder)
        else:
            for tag in self._tag_history:
                chip = QFrame()
                chip.setObjectName("douyinCommerceTagHistoryChip")
                chip_layout = QHBoxLayout(chip)
                chip_layout.setContentsMargins(6, 1, 3, 1)
                chip_layout.setSpacing(1)
                add_button = button(f"+ #{tag}", variant="ghost", compact=True)
                add_button.setObjectName("douyinCommerceTagHistoryAdd")
                add_button.setToolTip(f"添加历史标签 #{tag}")
                add_button.clicked.connect(
                    lambda _checked=False, value=tag: self.add_history_tag(value)
                )
                remove_button = button("×", variant="ghost", compact=True)
                remove_button.setObjectName("douyinCommerceTagHistoryRemove")
                remove_button.setToolTip(f"删除历史标签 #{tag}")
                remove_button.clicked.connect(
                    lambda _checked=False, value=tag: self.remove_history_tag(value)
                )
                chip_layout.addWidget(add_button)
                chip_layout.addWidget(remove_button, 0, Qt.AlignmentFlag.AlignTop)
                self.tag_history_layout.addWidget(chip)

    def _load_tag_history(self) -> None:
        try:
            self._tag_history = douyin_commerce_draft_service.list_tag_history()
        except Exception:
            # 历史标签只是本机效率能力，读取异常不能阻断内容准备或触发任何平台动作。
            self._tag_history = []
        self._render_tag_history()

    def _remember_current_tags(self) -> None:
        self._tag_history = douyin_commerce_draft_service.remember_tag_history(self._tags())
        self._render_tag_history()

    def _set_tags(self, values: object) -> None:
        if isinstance(values, list):
            normalized = []
            for value in values:
                for tag in self._parse_tag_text(value):
                    if tag not in normalized:
                        normalized.append(tag)
            self._tag_values = normalized
        else:
            self._tag_values = self._parse_tag_text(values)
        self._render_selected_tags()

    def add_tags_from_input(self) -> None:
        """把输入框中的一个或多个标签加入当前内容，不自动保存或上传。"""

        additions = self._parse_tag_text(self.tags_input.text())
        if not additions:
            return
        merged = list(self._tag_values)
        for tag in additions:
            if tag not in merged:
                merged.append(tag)
        self._tag_values = merged
        self.tags_input.blockSignals(True)
        self.tags_input.clear()
        self.tags_input.blockSignals(False)
        self._render_selected_tags()
        self._content_changed()

    def add_history_tag(self, tag: str) -> None:
        current = list(self._tag_values)
        if tag not in current:
            current.append(tag)
            self._tag_values = current
            self._render_selected_tags()
            self._content_changed()

    def remove_history_tag(self, tag: str) -> None:
        """删除一枚不再需要的本机历史标签，不影响当前内容或平台。"""

        try:
            self._tag_history = douyin_commerce_draft_service.remove_tag_history(tag)
        except Exception as exc:
            QMessageBox.warning(self, "删除最近标签", f"删除失败：{exc}")
            return
        self._render_tag_history()

    def remove_tag(self, tag: str) -> None:
        self._tag_values = [item for item in self._tag_values if item != tag]
        self._render_selected_tags()
        self._content_changed()

    def _sync_content_cards(self) -> None:
        """同步内容准备页的账号与视频摘要。"""

        account = self._selected_account() or {}
        account_name, subject = _account_identity(account)
        if account_name:
            self.account_identity_name.setText(account_name)
            self.account_card.setText(f"主体：{subject or '未设置主体'}")
            self.account_avatar.setText("")
            self.account_avatar.setPixmap(
                _account_avatar_pixmap(account, _ACCOUNT_AVATAR_SIZE)
            )
        else:
            self.account_identity_name.setText("尚未选择抖音账号")
            self.account_card.setText("请选择账号")
            self.account_avatar.setPixmap(QPixmap())
            self.account_avatar.setText("抖")
        self.account_combo.setVisible(True)

        video = self._selected_video() or {}
        video_metadata = media_service.video_display_metadata(video) if video else {}
        video_text = self._video_display(video, video_metadata)
        self.video_card.setText(video_text)
        self._sync_video_thumbnail(video)
        self.video_replace_button.setEnabled(self.video_combo.count() > 1 and not self._busy())

    @staticmethod
    def _video_display(media: dict, metadata: dict[str, str] | None = None) -> str:
        """以紧凑格式显示本机视频资料，不向用户暴露素材库内部状态。"""

        if not media:
            return "尚未选择视频素材"
        filename = _normalized(media.get("filename")) or "未命名视频"
        details = metadata or {}
        duration = _normalized(details.get("durationText"))
        resolution = _normalized(details.get("resolution"))
        facts = [item for item in (duration, resolution) if item]
        return filename if not facts else f"{filename}\n{' · '.join(facts)}"

    def _sync_video_thumbnail(self, media: dict) -> None:
        """显示素材库本机封面；缺失时仅生成或读取本地首帧，绝不写入平台封面。"""

        cover_path = ""
        if media:
            try:
                cover_path = media_service.cover_display_path(media)
            except Exception:
                _LOGGER.exception("读取本机视频缩略图失败")
        pixmap = QPixmap(cover_path) if cover_path else QPixmap()
        if not pixmap.isNull():
            scaled = pixmap.scaled(
                self.video_thumbnail.size(),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.video_thumbnail.setPixmap(scaled)
            self.video_thumbnail.setText("")
            self._set_widget_property(self.video_thumbnail, "hasPreview", True)
            return
        self.video_thumbnail.setPixmap(QPixmap())
        self.video_thumbnail.setText("视频")
        self._set_widget_property(self.video_thumbnail, "hasPreview", False)

    def _saved_content_payload(self) -> dict:
        """收集仅供本机恢复的内容准备字段，不带任何平台会话或选择结果。"""

        account = self._selected_account() or {}
        video = self._selected_video() or {}
        return {
            "accountId": account.get("id"),
            "accountFile": account.get("filePath"),
            "mediaId": video.get("id"),
            "mediaPath": video.get("storedPath"),
            "title": self.title_input.text(),
            "description": self.description_input.toPlainText(),
            "tags": self._tags(),
        }

    def _refresh_saved_content_status(self) -> None:
        """只读取本机保存状态；刷新页面时绝不触发平台或浏览器动作。"""

        try:
            saved = douyin_commerce_draft_service.load_content_draft()
        except douyin_commerce_draft_service.DouyinCommerceContentDraftError as exc:
            self._saved_content_available = False
            self.content_save_status.setText(f"已保存内容无法读取：{exc}")
            return
        self._saved_content_available = bool(saved)
        if saved:
            self.content_save_status.setText(
                f"已保存 · {saved.get('updatedAt') or '时间未知'}"
            )
        else:
            self.content_save_status.setText("尚未保存本地内容")

    def _refresh_batch_saved_content_status(self) -> None:
        """只读取批量草稿的本机状态，不触发平台、上传或恢复动作。"""

        try:
            saved = douyin_commerce_batch_draft_service.load_batch_draft()
        except Exception:
            self._batch_saved_content_available = False
            return
        self._batch_saved_content_available = bool(saved)

    @staticmethod
    def _restore_saved_combo(combo: QComboBox, *, identity: object, path: object) -> bool:
        expected_identity = _normalized(identity)
        expected_path = _normalized(path)
        combo.setCurrentIndex(0)
        for index in range(1, combo.count()):
            item = combo.itemData(index)
            if not isinstance(item, dict):
                continue
            current_identity = _normalized(item.get("id"))
            current_path = _normalized(item.get("filePath") or item.get("storedPath"))
            if expected_path and current_path == expected_path:
                combo.setCurrentIndex(index)
                return True
            if expected_identity and current_identity == expected_identity:
                combo.setCurrentIndex(index)
                return True
        return not expected_identity and not expected_path

    def save_content(self) -> None:
        """保存当前内容准备，严格只写本机 SQLite。"""

        try:
            saved = douyin_commerce_draft_service.save_content_draft(
                self._saved_content_payload()
            )
        except douyin_commerce_draft_service.DouyinCommerceContentDraftError as exc:
            QMessageBox.warning(self, "保存内容", str(exc))
            return
        try:
            self._remember_current_tags()
        except Exception:
            # 内容草稿已安全写入时，标签历史读取或更新失败不能把“保存内容”误报为失败。
            self._tag_history = []
            self._render_tag_history()
        self._saved_content_available = True
        self.content_save_status.setText(
            f"已保存 · {saved.get('updatedAt') or '刚刚'}"
        )
        QMessageBox.information(
            self,
            "内容已保存",
            "已保存当前账号、视频、标题、文案和话题到本机。\n不会上传、不保存平台草稿，也不会发布。",
        )
        self._sync_view()

    def restore_saved_content(self) -> None:
        """恢复上次本机内容准备；有临时编辑会话时拒绝覆盖。"""

        if self._session_id:
            QMessageBox.warning(
                self,
                "恢复已保存内容",
                "当前已有临时抖音编辑会话。请先放弃本次上传，再恢复已保存内容，避免覆盖已上传会话。",
            )
            return
        try:
            saved = douyin_commerce_draft_service.load_content_draft()
        except douyin_commerce_draft_service.DouyinCommerceContentDraftError as exc:
            QMessageBox.warning(self, "恢复已保存内容", str(exc))
            return
        if not saved:
            self._refresh_saved_content_status()
            QMessageBox.information(self, "恢复已保存内容", "当前没有可恢复的本机内容。")
            self._sync_view()
            return

        payload = dict(saved.get("payload") or {})
        self.account_combo.blockSignals(True)
        account_restored = self._restore_saved_combo(
            self.account_combo,
            identity=payload.get("accountId"),
            path=payload.get("accountFile"),
        )
        self.account_combo.blockSignals(False)
        self.video_combo.blockSignals(True)
        video_restored = self._restore_saved_combo(
            self.video_combo,
            identity=payload.get("mediaId"),
            path=payload.get("mediaPath"),
        )
        self.video_combo.blockSignals(False)
        self.title_input.blockSignals(True)
        self.title_input.setText(str(payload.get("title") or ""))
        self.title_input.blockSignals(False)
        self.tags_input.blockSignals(True)
        self.tags_input.clear()
        self.tags_input.blockSignals(False)
        self._set_tags(payload.get("tags") or [])
        self.description_input.blockSignals(True)
        self.description_input.setPlainText(str(payload.get("description") or ""))
        self.description_input.blockSignals(False)

        missing: list[str] = []
        if not account_restored and (payload.get("accountId") or payload.get("accountFile")):
            missing.append("已保存的抖音账号当前不在“状态正常”的账号列表中")
        if not video_restored and (payload.get("mediaId") or payload.get("mediaPath")):
            missing.append("已保存的视频素材当前不在素材管理中或文件已不存在")
        self._uploaded_editor_payload = None
        self._pending_upload_payload = None
        self._preflight_fingerprint = ""
        self._refresh_saved_content_status()
        self._load_tag_history()
        self.content_notice.setText(
            "内容已恢复，核对后上传。"
        )
        self.content_notice.setVisible(True)
        if missing:
            QMessageBox.warning(
                self,
                "部分内容待重新选择",
                "标题、文案和话题已恢复。\n\n" + "\n".join(missing),
            )
        self._sync_view()

    def _content_changed(self) -> None:
        # 用户重新编辑本机内容即表示准备再次同步；不能继续展示上次平台动作
        # 留下的“内容失败”，否则会把正常的标题、文案、标签编辑误判为失败。
        self._clear_stage_error("content")
        self._preflight_fingerprint = ""
        self._batch_preflight_fingerprint = ""
        self._sync_view()

    def clear_current_content(self) -> None:
        """清空当前表单，不删除可恢复的本地保存内容。"""

        if self._session_id:
            QMessageBox.warning(
                self,
                "清空当前信息",
                "当前已有临时抖音编辑会话。请先放弃本次上传，再清空当前信息。",
            )
            return
        if self._busy():
            QMessageBox.warning(self, "清空当前信息", "当前正在处理，请等待操作完成后再清空。")
            return

        self.account_combo.blockSignals(True)
        self.account_combo.setCurrentIndex(0)
        self.account_combo.blockSignals(False)
        self.video_combo.blockSignals(True)
        self.video_combo.setCurrentIndex(0)
        self.video_combo.blockSignals(False)
        self.title_input.blockSignals(True)
        self.title_input.clear()
        self.title_input.blockSignals(False)
        self.description_input.blockSignals(True)
        self.description_input.clear()
        self.description_input.blockSignals(False)
        self.tags_input.blockSignals(True)
        self.tags_input.clear()
        self.tags_input.blockSignals(False)
        self._set_tags([])
        # 批量工作台使用独立勾选列表；仅重置单选下拉框会留下旧的视频和地点。
        self.batch_video_list.blockSignals(True)
        for row in range(self.batch_video_list.count()):
            self.batch_video_list.item(row).setCheckState(Qt.CheckState.Unchecked)
        self.batch_video_list.blockSignals(False)
        self._selected_video_indexes = []
        self._batch_locations = {}
        self._batch_location_searches = {}
        self._batch_schedule_overrides = {}
        self._batch_location_feedback = ""
        # “清空当前内容”必须覆盖完整发布意图，不能只清标题/文案后继续沿用
        # 上一批的音乐、地点、声明或定时。候选与平台会话状态同样全部丢弃。
        self._clear_music_candidates()
        self._selected_music = None
        self.music_card.setText("尚未选择收藏音乐")
        self._locations = []
        self._selected_location_data = None
        self._pending_location = None
        self._location_applied = False
        self.location_result_list.clear()
        self.location_keyword.blockSignals(True)
        self.location_keyword.clear()
        self.location_keyword.blockSignals(False)
        self.location_scope_combo.blockSignals(True)
        self.location_scope_combo.setCurrentIndex(
            self.location_scope_combo.findData(
                douyin_commerce_service.LOCATION_SCOPE_DOMESTIC
            )
        )
        self.location_scope_combo.blockSignals(False)
        self.location_candidate_card.setText("尚未选择发布定位")
        self.location_applied_card.setText("尚未选择发布定位")
        self._clear_declaration("请选择作品内容声明")
        self.batch_location_commission_combo.blockSignals(True)
        self.batch_location_commission_combo.setCurrentIndex(
            self.batch_location_commission_combo.findData(
                DEFAULT_COMMISSION_FILTER
            )
        )
        self.batch_location_commission_combo.blockSignals(False)
        self.batch_location_scope_combo.blockSignals(True)
        self.batch_location_scope_combo.setCurrentIndex(
            self.batch_location_scope_combo.findData(
                douyin_commerce_service.LOCATION_SCOPE_DOMESTIC
            )
        )
        self.batch_location_scope_combo.blockSignals(False)
        self.batch_location_keyword.blockSignals(True)
        self.batch_location_keyword.clear()
        self.batch_location_keyword.blockSignals(False)
        self.batch_publish_mode.blockSignals(True)
        self.batch_publish_mode.setCurrentIndex(
            self.batch_publish_mode.findData("immediate")
        )
        self.batch_publish_mode.blockSignals(False)
        self.batch_timer_enabled.blockSignals(True)
        self.batch_timer_enabled.setChecked(False)
        self.batch_timer_enabled.blockSignals(False)
        self.timer_enabled.blockSignals(True)
        self.timer_enabled.setChecked(False)
        self.timer_enabled.blockSignals(False)
        self._sync_batch_video_status()
        self._uploaded_editor_payload = None
        self._pending_upload_payload = None
        self._preflight_fingerprint = ""
        self._batch_preflight_fingerprint = ""
        self._clear_stage_error("content")
        self._sync_content_cards()
        self.content_notice.setText("当前信息已清空；已保存的本地内容仍可恢复。")
        self.content_notice.setVisible(True)
        self._sync_view()

    @staticmethod
    def _upload_identity(payload: dict) -> tuple[str, str]:
        """仅账号和视频决定是否必须重新建立编辑会话。"""

        accounts = payload.get("accountList") or [""]
        files = payload.get("fileList") or [""]
        return (
            _normalized(accounts[0] if accounts else ""),
            _normalized(files[0] if files else ""),
        )

    @staticmethod
    def _content_fields(payload: dict) -> tuple[str, str, tuple[str, ...]]:
        """标题、文案和标签可以在同一编辑会话内同步。"""

        return (
            _normalized(payload.get("title")),
            str(payload.get("description") or ""),
            tuple(
                _normalized(tag).lstrip("#")
                for tag in payload.get("tags") or []
                if _normalized(tag).lstrip("#")
            ),
        )

    def _content_change_kind(self) -> str:
        """返回本次内容页操作需要的最小平台动作。"""

        if not self._session_id:
            return "reupload"
        # 兼容升级前仍在进程内的编辑会话：其安全校验继续由会话管理器完成；
        # 新会话均会在上传成功时写入快照，获得精确的同步/重传分流。
        if not self._uploaded_editor_payload:
            return "none"
        try:
            current = self.collect_upload_payload()
        except (ValueError, douyin_commerce_service.DouyinCommerceError):
            return "reupload"
        if self._upload_identity(current) != self._upload_identity(
            self._uploaded_editor_payload
        ):
            return "reupload"
        if self._content_fields(current) != self._content_fields(
            self._uploaded_editor_payload
        ):
            return "sync"
        return "none"

    def _batch_content_change_kind(self) -> str:
        """判断批量页是否仍持有可以复用的编辑会话。

        批量预检/提交默认会逐条关闭会话，所以没有活会话时明确返回
        ``reupload``。只有同一账号、同一首视频的会话仍在且共享文本变更时，
        才允许调用 ``synchronize_content``；不会把关闭会话误报为无需上传。
        """

        if not self._session_id or not self._uploaded_editor_payload:
            return "reupload"
        try:
            current = self.collect_upload_payload()
        except (ValueError, douyin_commerce_service.DouyinCommerceError):
            return "reupload"
        if self._upload_identity(current) != self._upload_identity(
            self._uploaded_editor_payload
        ):
            return "reupload"
        if self._content_fields(current) != self._content_fields(
            self._uploaded_editor_payload
        ):
            return "sync"
        return "none"

    def _schedule_changed(self) -> None:
        self._preflight_fingerprint = ""
        self._batch_preflight_fingerprint = ""
        if self.selected_video_count() >= 1:
            self._render_batch_item_rows()
        self._sync_view()

    @staticmethod
    def _default_schedule_datetime(now: datetime | None = None) -> datetime:
        """返回北京时间次日 16:00 的默认定时点。"""

        current = now or datetime.now(_SHANGHAI_TZ)
        if current.tzinfo is None:
            current = current.replace(tzinfo=_SHANGHAI_TZ)
        else:
            current = current.astimezone(_SHANGHAI_TZ)
        return (current + timedelta(days=1)).replace(
            hour=16,
            minute=0,
            second=0,
            microsecond=0,
        )

    def _refresh_schedule_default(self) -> None:
        """保持默认日期不落后当前北京时间，不覆盖今天或未来的手选日期。"""

        today = datetime.now(_SHANGHAI_TZ).date()
        minimum = QDate(today.year, today.month, today.day)
        default_at = self._default_schedule_datetime()
        default = QDate(default_at.year, default_at.month, default_at.day)
        is_stale_default = self.schedule_date.date() < minimum
        self.schedule_date.setMinimumDate(minimum)
        if self._schedule_date_auto_default and is_stale_default:
            blocked = self.schedule_date.blockSignals(True)
            self.schedule_date.setDate(default)
            self.schedule_date.blockSignals(blocked)

    def pause_batch_publish(self) -> None:
        """请求批量在当前视频完成后停下，未开始视频保持未开始。"""

        if not self.runner.is_running(_BATCH_RUN_KEY):
            return
        answer = QMessageBox.question(
            self,
            "确认暂停发布",
            "抖音验证成功后会自动继续发布。是否仍要在当前视频完成后暂停剩余视频？",
            QMessageBox.StandardButton.No | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self._batch_executor.request_pause(source="user_confirmed"):
            self._batch_pause_requested = True
            task_id = int(self._batch_task_id or 0)
            if task_id > 0:
                try:
                    task_service.record_task_event(
                        task_id,
                        "batch_pause_requested",
                        "用户已在客户端确认：当前视频完成后暂停后续发布",
                        level="warning",
                    )
                except Exception:
                    _LOGGER.warning("抖音带货暂停请求已生效，但审计事件未能写入")
            self._sync_view()
            self.validation_label.setText("已请求暂停；当前视频完成后不会再提交后续视频。")

    def _timer_enabled_changed(self, _checked: bool) -> None:
        """切换发布方式只影响本次预检指纹，不触发任何平台动作。"""

        self._preflight_fingerprint = ""
        self._batch_preflight_fingerprint = ""
        if self.timer_enabled.isChecked():
            self._refresh_schedule_default()
        if self.selected_video_count() >= 1 and hasattr(self, "batch_publish_mode"):
            target = "interval-schedule" if self.timer_enabled.isChecked() else "immediate"
            if self.batch_publish_mode.currentData() != target:
                self.batch_publish_mode.setCurrentIndex(
                    self.batch_publish_mode.findData(target)
                )
        self._sync_view()

    def _location_keyword_edited(self) -> None:
        if self.location_result_list.selectedItems():
            self.location_result_list.blockSignals(True)
            self.location_result_list.clearSelection()
            self.location_result_list.blockSignals(False)
        self._locations = []
        self._selected_location_data = None
        self.location_result_list.clear()
        self._location_applied = False
        self.location_status.setText("关键词已修改，请从当前抖音编辑页重新搜索发布定位")
        self.location_candidate_card.setText("位置搜索词已修改，请重新搜索并选择平台候选")
        self._preflight_fingerprint = ""
        self._sync_view()

    def _location_scope_changed(self) -> None:
        """范围变更必须使旧地点和声明失效，避免跨范围误复用。"""

        if self.location_result_list.selectedItems():
            self.location_result_list.blockSignals(True)
            self.location_result_list.clearSelection()
            self.location_result_list.blockSignals(False)
        self._locations = []
        self._selected_location_data = None
        self.location_result_list.clear()
        self._location_applied = False
        self.location_candidate_card.setText("地点范围已变更，请重新搜索并选择平台候选")
        scope = self._selected_location_scope()
        if scope:
            self.location_status.setText(
                f"已选择“{douyin_commerce_service.location_scope_label(scope)}”范围，请输入地点后搜索"
            )
        else:
            self.location_status.setText("请先选择地点范围：本地或国内")
        self._preflight_fingerprint = ""
        self._sync_view()

    def _location_selected(self) -> None:
        selected = self.location_result_list.selectedItems()
        data = selected[0].data(Qt.ItemDataRole.UserRole) if selected else None
        location = dict(data) if isinstance(data, dict) else None
        if not location:
            self.location_candidate_card.setText("尚未选择发布定位")
            self._pending_location = None
        else:
            self._pending_location = dict(location)
            self.location_candidate_card.setText(
                f"{self._location_display(location)}\n正在写入平台并回读"
            )
        self._preflight_fingerprint = ""
        self._sync_view()
        if location:
            if self._setup_generation_id:
                self._stage_location_selection(location)
            else:
                self._start_location_write(location)

    def change_location_selection(self) -> None:
        """放弃已选地点，要求从当前编辑页重新读取候选。"""

        self.location_result_list.blockSignals(True)
        self.location_result_list.clearSelection()
        self.location_result_list.setCurrentRow(-1)
        self.location_result_list.clear()
        self.location_result_list.blockSignals(False)
        # 抖音在选择地点后会关闭候选下拉层。不能把上次的本机候选继续展示成
        # 当前可点击项，否则再次选择时页面找不到同一节点并产生假失败。
        self._locations = []
        self._selected_location_data = None
        self._pending_location = None
        self._location_applied = False
        self.location_candidate_card.setText("请重新搜索并选择平台发布定位候选")
        self.location_status.setText("已放弃当前定位，请重新搜索后选择。")
        self._preflight_fingerprint = ""
        self.location_view_stack.setCurrentWidget(self.location_search_view)
        self._sync_view()

    def _music_combo_activated(self, index: int) -> None:
        """首次打开即读取收藏列表，后续选择直接写入当前编辑会话。"""

        candidate = self.music_combo.itemData(index)
        if not isinstance(candidate, dict):
            self._load_favorite_music_candidates()
            return
        if self._setup_generation_id:
            self._stage_music_selection(candidate)
            return
        if self.selected_video_count() >= 1:
            self._select_batch_music_locally(candidate)
            return
        self._start_music_write(dict(candidate))

    def _select_batch_music_locally(self, candidate: dict[str, str]) -> None:
        """保存批量共享音乐选择，不在内容准备阶段创建或写入编辑会话。"""

        self._stage_music_selection(candidate)

    def _stage_music_selection(self, candidate: dict[str, str]) -> None:
        """仅保存音乐公开身份，发布执行时再进入正式页核验。"""

        if not isinstance(candidate, dict):
            return
        staged = {
            key: _normalized(candidate.get(key))
            for key in ("musicId", "title", "creator", "duration")
        }
        if not staged["musicId"] and not staged["title"]:
            return
        self._selected_music = staged
        self._pending_music = None
        self._staged_music_confirmed = True
        self._batch_preflight_fingerprint = ""
        self._preflight_fingerprint = ""
        self._restore_music_combo(self._selected_music)
        self.music_status.setText("本地已选择，发布时重新核验")
        self.music_status.setVisible(True)
        self._sync_view()

    def _stage_location_selection(self, candidate: dict[str, str]) -> None:
        """只暂存完整 POI 公开字段，不在采集页调用 apply_location。"""

        if not isinstance(candidate, dict):
            return
        scope = _normalized(candidate.get("locationScope")) or self._selected_location_scope()
        if scope not in {
            douyin_commerce_service.LOCATION_SCOPE_DOMESTIC,
            douyin_commerce_service.LOCATION_SCOPE_LOCAL,
        }:
            return
        staged = {
            key: _normalized(candidate.get(key))
            for key in ("poiId", "name", "address", "distance")
        }
        if not staged["poiId"] or not staged["name"] or not staged["address"]:
            return
        staged["locationScope"] = scope
        self._selected_location_data = staged
        self._pending_location = None
        self._location_applied = False
        self._staged_location_confirmed = True
        self.location_candidate_card.setText(self._location_display(staged))
        self.location_status.setText("本地已选择，发布时重新核验")
        self._preflight_fingerprint = ""
        self._sync_view()

    def _toggle_music_candidate_list(self) -> None:
        """在音乐卡内展开候选，避免原生下拉浮层遮挡声明区。"""

        if not self._music_candidates:
            self._load_favorite_music_candidates()
            return
        self.music_candidate_list.setVisible(not self.music_candidate_list.isVisible())

    def _music_candidate_clicked(self, item: QListWidgetItem) -> None:
        """候选点击后立即收起列表并写入当前抖音编辑会话。"""

        data = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict):
            return
        self.music_candidate_list.setVisible(False)
        if self._setup_generation_id:
            self._stage_music_selection(data)
            return
        if self.selected_video_count() >= 1:
            self._select_batch_music_locally(data)
            return
        self._start_music_write(dict(data))

    def _declaration_toggled(self, declaration: str, checked: bool) -> None:
        """批量仅保存声明；单视频仍在当前编辑会话即时写入。"""

        if not checked or self._suppress_declaration_signal:
            return
        if self._setup_generation_id:
            self._stage_declaration_selection(declaration)
            return
        if self.selected_video_count() >= 1:
            self._stage_declaration_selection(declaration)
            return
        self._start_declaration_write(declaration)

    def _stage_declaration_selection(self, value: str) -> None:
        """规范化并保存本批声明，不调用旧编辑会话写入接口。"""

        declaration = _normalized(value)
        if declaration not in self.declaration_buttons:
            return
        self._set_selected_declaration(declaration)
        self._confirmed_declaration = declaration
        self._pending_declaration = ""
        self._declaration_applied = False
        self._staged_declaration_confirmed = True
        self._batch_preflight_fingerprint = ""
        self._preflight_fingerprint = ""
        self.declaration_status.setText("本地已选择，发布时重新核验")
        self._sync_view()

    def _clear_declaration(self, message: str) -> None:
        self._set_selected_declaration(self._DEFAULT_CONTENT_DECLARATION)
        self._declaration_applied = False
        self._confirmed_declaration = ""
        self._pending_declaration = ""
        self.declaration_card.setText("默认声明尚未写入平台")
        self.declaration_status.setText(message)

    @staticmethod
    def _location_display(location: dict[str, str]) -> str:
        address = _normalized(location.get("address")) or "平台未返回完整地址"
        distance = _normalized(location.get("distance"))
        suffix = f" · {distance}" if distance else ""
        return f"{location.get('name') or ''}\n{address}{suffix}"

    @staticmethod
    def _music_display(music: dict[str, str]) -> str:
        return (
            f"{music.get('title') or ''}\n"
            f"{music.get('creator') or '平台未返回作者'} · {music.get('duration') or ''}"
        )

    def _go_to_step(self, step: int) -> None:
        if self.selected_video_count() >= 1:
            if step == 1 and not self._batch_content_is_valid():
                QMessageBox.warning(self, "抖音带货", "请先选择账号、1 至 20 条视频并填写文案。")
                return
            if step == 2 and not self._can_batch_review():
                QMessageBox.warning(self, "抖音带货", "请先选择收藏音乐、作品内容声明和每条地点预设。")
                return
            self.pages.setCurrentIndex(step)
            self._sync_view()
            return
        if step == 1 and not self._session_id:
            QMessageBox.warning(self, "抖音带货", "请先完成内容准备并上传视频。")
            return
        if step == 2 and not self._can_review():
            timer_hint = "并设置未来定时" if self.timer_enabled.isChecked() else ""
            QMessageBox.warning(self, "抖音带货", f"请先完成作品内容声明{timer_hint}。")
            return
        self.pages.setCurrentIndex(step)
        self._sync_view()

    def continue_to_review(self) -> None:
        """检查前先把客户端定时与同一编辑页实际状态对齐。"""

        if self.selected_video_count() >= 1:
            self._go_to_step(2)
            return

        if not self._can_review():
            self._go_to_step(2)
            return
        try:
            payload = self.collect_payload("preflight")
        except Exception as exc:
            self._platform_action_error("schedule", exc)
            return
        session_id = self._session_id
        if not session_id:
            self._go_to_step(2)
            return
        expected_schedule = _normalized(payload.get("scheduleTime"))
        self.platform_review_status.setText("正在核对发布时间")
        self._start_immediate_write(
            "schedule",
            lambda: douyin_commerce_session.commerce_session_manager.sync_schedule(
                session_id, payload
            ),
            lambda result: self._schedule_sync_succeeded(result, expected_schedule),
            self._schedule_sync_failed,
        )

    def _schedule_sync_succeeded(self, result: object, expected_schedule: str) -> None:
        """仅在平台回读与本次选择一致后进入检查页。"""

        actual_schedule = _normalized(
            result.get("scheduledAt") if isinstance(result, dict) else ""
        )
        if expected_schedule:
            matched = actual_schedule == expected_schedule
        else:
            matched = not actual_schedule
        if not matched:
            self._schedule_sync_failed("抖音页面回读的发布时间与当前设置不一致")
            return
        self._preflight_fingerprint = ""
        self._clear_stage_error("schedule")
        self._go_to_step(2)

    def _schedule_sync_failed(self, message: str) -> None:
        self.platform_review_status.setText("发布时间未同步")
        self._platform_action_error("schedule", message)

    def _set_button_variant(self, control, variant: str) -> None:
        if control.property("variant") == variant:
            return
        control.setProperty("variant", variant)
        control.style().unpolish(control)
        control.style().polish(control)

    @staticmethod
    def _set_widget_property(control, name: str, value: object) -> None:
        """只在状态变化时重刷 Qt 样式，避免高频刷新造成界面闪烁。"""

        if control.property(name) == value:
            return
        control.setProperty(name, value)
        control.style().unpolish(control)
        control.style().polish(control)

    def _session_status_hint(self) -> str:
        if self._busy():
            return "正在与抖音编辑页同步"
        if self._session_id:
            return "同一抖音编辑会话"
        if self._saved_content_available:
            return "已保存本机内容"
        return "本机内容准备"

    def _current_platform_stage(self) -> str:
        """返回下一个必须获得平台回读的设置阶段。

        这不是根据控件是否可见来推断，而是只依据同一编辑会话中的已回读状态，
        因此页面可以稳定地把用户带回真正需要恢复的步骤。
        """

        if not self._session_id or self._content_change_kind() != "none":
            return "blocked"
        if self._selected_music is None:
            return "music"
        if not self._location_applied:
            return "location"
        if not self._declaration_applied:
            return "declaration"
        return "schedule"

    def _stage_error_label(self, stage: str) -> QLabel:
        """创建一个阶段内的可恢复提示，绝不把自动化细节展示给用户。"""

        if stage not in self._STAGE_ERROR_COPY:
            raise ValueError(f"未知的抖音带货错误阶段：{stage}")
        label = QLabel()
        label.setObjectName("douyinCommerceStageError")
        label.setWordWrap(True)
        label.setVisible(False)
        self._stage_error_labels[stage] = label
        return label

    def _set_stage_error(self, stage: str, diagnostic: object) -> None:
        """在当前阶段保留用户可执行的恢复提示，并仅在本机记录诊断。"""

        if stage not in self._STAGE_ERROR_COPY:
            raise ValueError(f"未知的抖音带货错误阶段：{stage}")
        reason, next_action = self._STAGE_ERROR_COPY[stage]
        _LOGGER.warning(
            "抖音带货阶段失败 stage=%s diagnostic=%s",
            stage,
            _normalized(diagnostic),
        )
        label = self._stage_error_labels.get(stage)
        if label is not None:
            label.setText(f"失败原因：{reason}\n下一步：{next_action}")
            label.setVisible(True)

    def _clear_stage_error(self, stage: str) -> None:
        label = self._stage_error_labels.get(stage)
        if label is not None:
            label.clear()
            label.setVisible(False)

    def _platform_action_error(self, stage: str, diagnostic: object) -> None:
        """把执行器错误留在本机日志，并把用户停在准确的恢复阶段。"""

        self._set_stage_error(stage, diagnostic)
        self._sync_view()

    def _start_immediate_write(self, kind: str, work, on_success, on_error) -> bool:
        """串行写入同一抖音编辑页，成功前不把客户端暂选当作平台状态。"""

        if (
            not self._session_id
            or self._immediate_write_kind
            or self.runner.is_running(self._IMMEDIATE_WRITE_KEY)
        ):
            return False
        self._immediate_write_kind = kind
        self._sync_view()
        started = self.runner.run(
            self._IMMEDIATE_WRITE_KEY,
            work,
            on_success=on_success,
            on_error=on_error,
            on_finished=lambda: self._finish_immediate_write(kind),
        )
        if not started:
            self._immediate_write_kind = ""
            self._sync_view()
        return started

    def _finish_immediate_write(self, kind: str) -> None:
        if self._immediate_write_kind == kind:
            self._immediate_write_kind = ""
        self._sync_view()
        if (
            kind in {"music_read", "music_refresh"}
            and self._open_music_picker_after_load
            and self._music_candidates
        ):
            self._open_music_picker_after_load = False
            QTimer.singleShot(0, self._open_music_picker_if_ready)
        if kind == "declaration" and self._cache_load_pending:
            self._cache_load_pending = False
            QTimer.singleShot(0, self._load_cached_favorite_music_candidates)

    def _open_music_picker_if_ready(self) -> None:
        """仅在读取完成且控件恢复可用后展开同一音乐下拉框。"""

        if self.music_combo.isEnabled() and self._music_candidates:
            self.music_combo.showPopup()

    def _sync_view(self) -> None:
        """兼容既有信号/回调入口，统一委托给工作台状态投影。"""

        self._sync_workbench()

    def _sync_workbench(self) -> None:
        current_step = self.pages.currentIndex()
        self._refresh_schedule_default()
        batch_mode = self.selected_video_count() >= 1
        for index, label in enumerate(self.step_labels):
            if index == current_step:
                state = "active"
            elif self._step_complete(index):
                state = "complete"
            else:
                state = "pending"
            self._set_widget_property(label, "stepState", state)
            self._set_widget_property(self.step_cards[index], "stepState", state)
        self.progress_context_label.setText(self._STEP_HINTS[current_step])
        if hasattr(self, "review_back_button"):
            has_editor_session = bool(self._session_id or self._setup_generation_id)
            self.review_back_button.setText(
                "返回平台设置" if has_editor_session else "开始新内容"
            )
            self.review_back_button.setEnabled(not self._busy())
        if hasattr(self, "pause_batch_button"):
            batch_running = bool(self._batch_task_id) and self.runner.is_running(
                _BATCH_RUN_KEY
            )
            self.pause_batch_button.setVisible(batch_running)
            self.pause_batch_button.setEnabled(
                batch_running and not self._batch_pause_requested
            )
            self.pause_batch_button.setText(
                "将在当前视频后暂停" if self._batch_pause_requested else "暂停发布"
            )
        self._sync_content_cards()

        if batch_mode and self._batch_editor_session_ended and not self._session_id:
            self.content_notice.setText(self._BATCH_EDITOR_SESSION_ENDED_HINT)
            self.content_notice.setVisible(True)

        upload_ready = self._content_is_valid()
        self.upload_button.setEnabled(upload_ready and not self._busy())
        content_change_kind = self._content_change_kind()
        if not self._session_id:
            self.upload_button.setText("上传视频并继续")
        elif content_change_kind == "sync":
            self.upload_button.setText("同步内容并继续")
        elif content_change_kind == "reupload":
            self.upload_button.setText("重新上传视频并继续")
        else:
            self.upload_button.setText("继续平台设置")
        self.operation_dock.setVisible(current_step == 0)
        self._set_button_variant(
            self.content_stage_button,
            "primary" if current_step == 0 else "secondary",
        )
        self._set_button_variant(
            self.platform_stage_button,
            "primary" if current_step == 1 else "secondary",
        )
        self.content_stage_button.setEnabled(not self._busy())
        self.platform_stage_button.setEnabled(
            bool(self._session_id or self._setup_generation_id) and not self._busy()
        )
        if current_step == 0:
            if self._busy():
                dock_state = "running"
                dock_detail = "正在上传，请稍候。"
                dock_status = "正在上传"
            elif self._session_id and self._content_change_kind() == "sync":
                dock_state = "warning"
                dock_detail = "内容已修改，点击同步后继续。"
                dock_status = "待同步"
            elif self._session_id and self._content_change_kind() == "reupload":
                dock_state = "warning"
                dock_detail = "账号或视频已变更，需要重新上传。"
                dock_status = "需重传"
            elif self._session_id:
                dock_state = "ready"
                dock_detail = "内容未变更，可继续平台设置。"
                dock_status = "可继续"
            elif upload_ready:
                dock_state = "ready"
                dock_detail = "上传后设置音乐、地点、声明和定时。"
                dock_status = "可上传"
            else:
                dock_state = "pending"
                dock_detail = "补齐账号、视频、标题和文案。"
                dock_status = "待补全"
            self.operation_dock_detail.setText(dock_detail)
            self.operation_dock_status.setText(dock_status)
            self._set_widget_property(
                self.operation_dock_status, "operationState", dock_state
            )
        self.abandon_button.setEnabled(
            bool(self._session_id or self._setup_generation_id) and not self._busy()
        )
        self.abandon_button.setVisible(bool(self._session_id or self._setup_generation_id))
        self.save_content_button.setEnabled(not self._busy())
        self.restore_content_button.setEnabled(
            self._saved_content_available and not self._session_id and not self._busy()
        )
        self.clear_content_button.setEnabled(not self._session_id and not self._busy())
        can_edit_batch_content = not self._session_id and not self._busy()
        self.select_all_videos_button.setEnabled(can_edit_batch_content)
        self.clear_video_selection_button.setEnabled(can_edit_batch_content)
        self.batch_save_content_button.setEnabled(not self._busy())
        self.batch_restore_content_button.setEnabled(
            self._batch_saved_content_available and can_edit_batch_content
        )
        self.batch_clear_content_button.setEnabled(can_edit_batch_content)

        self._sync_platform_workspace()
        summary = self._summary()
        for key, value in self.summary_values.items():
            value.setText(summary.get(key) or "待完成")
        self._render_batch_review_rows()
        # 一条视频同样必须走批量工作台。这样单条和 1..20 条视频不会分裂为
        # 两套上传/验证实现，也不会悄悄回退到旧单视频页面。
        if batch_mode:
            try:
                batch_payload = self.collect_batch_payload()
                valid = self._can_batch_review()
                if self._busy() and self._batch_progress_text:
                    validation = self._batch_progress_text
                elif self._batch_result_feedback:
                    validation = self._batch_result_feedback
                else:
                    validation = "确认信息后提交发布。" if valid else "请补齐音乐、声明和每条完整地点。"
            except Exception as exc:
                batch_payload = None
                valid = False
                validation = str(exc)
            self.validation_label.setText(validation)
            submit_ready = bool(valid and batch_payload and not self._busy())
            self.preflight_button.setVisible(False)
            self.preflight_button.setEnabled(False)
            self.submit_button.setEnabled(submit_ready)
            self.submit_button.setText(f"确认提交 {self.selected_video_count()} 条视频")
            self._set_button_variant(self.submit_button, "primary" if submit_ready else "secondary")
            return
        self.preflight_button.setVisible(False)
        try:
            self.collect_payload("publish")
            valid = self._can_review()
            if valid:
                validation = "确认信息后提交发布。"
            else:
                validation = (
                    "请先完成视频上传、作品内容声明与未来定时。"
                    if self.timer_enabled.isChecked()
                    else "请先完成视频上传与作品内容声明。"
                )
        except (ValueError, douyin_commerce_service.DouyinCommerceError) as exc:
            valid = False
            validation = str(exc)
        if self._session_id and self._content_change_kind() != "none":
            valid = False
            validation = "内容已变更，请先同步内容或重新上传后再继续。"
        self.validation_label.setText(validation)
        submit_ready = bool(valid and bool(self._session_id) and not self._busy())
        self.preflight_button.setEnabled(False)
        self.submit_button.setEnabled(submit_ready)
        self.submit_button.setText("确认定时提交" if self.timer_enabled.isChecked() else "确认立即发表")
        self._set_button_variant(self.submit_button, "primary" if submit_ready else "secondary")

        if self._busy():
            self.status_badge.setText("正在处理")
        elif submit_ready:
            self.status_badge.setText("可以提交")
        elif self._session_id:
            self.status_badge.setText("继续配置")
        else:
            self.status_badge.setText("等待内容准备")
        if self._busy():
            self.reference_save_badge.setText("正在处理")
            self._set_widget_property(self.reference_save_badge, "saveState", "busy")
        elif self._session_id:
            self.reference_save_badge.setText("平台设置进行中")
            self._set_widget_property(self.reference_save_badge, "saveState", "session")
        elif self._saved_content_available:
            self.reference_save_badge.setText("本地内容已保存")
            self._set_widget_property(self.reference_save_badge, "saveState", "saved")
        else:
            self.reference_save_badge.setText("本地内容未保存")
            self._set_widget_property(self.reference_save_badge, "saveState", "empty")
        self.session_hint_label.setText(self._session_status_hint())
        if self._busy():
            self.hero_detail_label.setText("正在与当前抖音编辑页同步。请等待本步完成，避免重复点击。")
        elif self._session_id:
            self.hero_detail_label.setText(
                "已建立同一抖音编辑会话。后续步骤不会重复上传视频，也不会保存平台草稿。"
            )
        elif self._saved_content_available:
            self.hero_detail_label.setText("已检测到本机保存内容。恢复后仍需由你手动上传视频。")
        else:
            self.hero_detail_label.setText("先完成本机内容准备；此时不会访问抖音平台。")

    def _sync_platform_workspace(self) -> None:
        """把当前会话投影到固定双栏；四项可独立填写，写入动作串行回读。"""

        busy = self._busy()
        batch_mode = self.selected_video_count() >= 1
        if self.retry_collector_button.isVisible():
            self.retry_collector_button.setEnabled(not busy)
        if hasattr(self, "batch_item_settings_stage"):
            self.batch_item_settings_stage.setVisible(batch_mode)
            self.location_stage.setVisible(not batch_mode)
            self.schedule_stage.setVisible(True)
            self.platform_execution_log.setVisible(False)
            if batch_mode:
                self._sync_batch_location_controls()
                if self._batch_item_rows_signature != self._batch_item_rows_state_signature():
                    self._render_batch_item_rows()
        session_ready = bool(self._setup_generation_id) or (
            bool(self._session_id) and self._content_change_kind() == "none"
        )
        self.platform_back_button.setEnabled(not busy)
        if self._session_id and self._content_change_kind() == "sync":
            self.platform_session_status.setText("内容已修改，请返回内容同步")
        elif self._session_id and self._content_change_kind() == "reupload":
            self.platform_session_status.setText("账号或视频已变更，请重新上传")
        elif busy:
            self.platform_session_status.setText("正在处理，请稍候")
        elif batch_mode and self._batch_editor_session_ended:
            self.platform_session_status.setText(self._BATCH_EDITOR_SESSION_ENDED_HINT)
        elif session_ready:
            self.platform_session_status.setText("编辑会话已就绪")
        else:
            self.platform_session_status.setText("等待上传视频")
        session_status_visible = (
            busy
            or (bool(self._session_id) and not session_ready)
            or (batch_mode and self._batch_editor_session_ended)
        )
        self.platform_session_status.setVisible(False)

        # 批量中的“选择音乐”只保存本地批次配置；预检时才会逐条写入平台并回读。
        # 批量已缓存的音乐可以在没有设置会话时直接选择；这一步完全不访问平台。
        # “刷新收藏”仍需设置会话，因为它会主动读取一次当前抖音收藏抽屉。
        can_choose_music = (
            session_ready
            or (batch_mode and bool(self._selected_account()))
        ) and not busy
        self.music_combo.setEnabled(can_choose_music)
        self.music_refresh_button.setEnabled(can_choose_music)
        if not busy:
            if batch_mode and self._selected_music:
                self.music_status.setText(
                    "本地已选择，发布时重新核验"
                    if self._staged_music_confirmed
                    else ""
                )
            elif not session_ready:
                self.music_status.setText("上传后选择")
            elif self._selected_music:
                self.music_status.setText("")
            elif self._music_candidates:
                self.music_status.setText("直接选择一首收藏音乐")
            else:
                self.music_status.setText("暂无本地收藏音乐，可点击刷新")
        self.music_status.setVisible(
            self._staged_music_confirmed
            or self._immediate_write_kind
            in {"music", "music_read", "music_cache", "music_refresh"}
        )
        self.music_card.setVisible(False)

        selected_location = self._selected_location()
        can_search_location = (
            session_ready
            and not self._location_applied
            and not busy
        )
        self.location_scope_combo.setEnabled(can_search_location)
        self.location_keyword.setEnabled(can_search_location)
        self.location_search_button.setEnabled(
            can_search_location and bool(self._selected_location_scope())
        )
        self.location_result_list.setVisible(
            bool(self._locations)
            and selected_location is None
            and self._pending_location is None
        )
        self.change_location_button.setEnabled(
            bool(self._location_applied and selected_location) and not busy
        )
        self._set_button_variant(
            self.location_search_button,
            "primary" if can_search_location else "secondary",
        )
        if not busy:
            if not session_ready:
                self.location_status.setText("完成上传后可选择")
            elif self._location_applied:
                self.location_status.setText("地点已确认")
            elif self._pending_location:
                self.location_status.setText("正在确认地点")
            elif self._locations:
                self.location_status.setText("请选择一个地点")
            else:
                self.location_status.setText("搜索地点")
        self._sync_location_view()

        can_choose_declaration = (session_ready or batch_mode) and not busy
        for declaration, option in self.declaration_buttons.items():
            option.setEnabled(
                can_choose_declaration
                and declaration not in self._DISABLED_CONTENT_DECLARATIONS
            )
        if batch_mode:
            self.declaration_status.setText("")
        elif not busy:
            if not can_choose_declaration:
                self.declaration_status.setText("上传后选择")
            elif self._declaration_applied:
                self.declaration_status.setText("")
            else:
                self.declaration_status.setText("选择后立即写入")
        self.declaration_status.setVisible(
            not batch_mode and self._immediate_write_kind == "declaration"
        )
        self.declaration_card.setVisible(False)

        # 定时仅是本地批次配置，不必等待音乐或地点；单条仍要求当前编辑会话存在。
        can_configure_schedule = (session_ready or batch_mode) and not busy
        self.timer_enabled.setEnabled(can_configure_schedule)
        self.schedule_date.setEnabled(
            can_configure_schedule and self.timer_enabled.isChecked()
        )
        self.schedule_time.setEnabled(
            can_configure_schedule and self.timer_enabled.isChecked()
        )
        self.batch_schedule_controls.setVisible(
            batch_mode and self.timer_enabled.isChecked()
        )
        if not can_configure_schedule:
            self.publish_mode_hint.setText("上传后设置")
        elif self.timer_enabled.isChecked():
            self.publish_mode_hint.setText("定时：北京时间")
        else:
            self.publish_mode_hint.setText("立即发表")

        review_ready = (self._can_batch_review() if batch_mode else self._can_review()) and not busy
        self.to_review_button.setEnabled(review_ready)
        self._set_button_variant(
            self.to_review_button, "primary" if review_ready else "secondary"
        )
        checking_schedule = self._immediate_write_kind == "schedule"
        self.to_review_button.setText("正在检查…" if checking_schedule else "检查并继续")
        if self._setup_close_error_code:
            self.platform_review_status.setText(
                "平台设置采集器关闭未完成 · 错误码 "
                f"{self._setup_close_error_code}"
            )
        elif self._setup_start_error_code:
            self.platform_review_status.setText(
                "平台设置采集器未就绪 · 错误码 "
                f"{self._setup_start_error_code}"
            )
        elif checking_schedule:
            self.platform_review_status.setText("正在检查发布时间…")
        elif session_status_visible:
            self.platform_review_status.setText(self.platform_session_status.text())
        elif review_ready:
            self.platform_review_status.setText("已完成，可检查")
        elif batch_mode:
            self.platform_review_status.setText("补齐音乐、声明和每条地点后可检查")
        elif not session_ready:
            self.platform_review_status.setText("上传视频后继续")
        elif self._selected_music is None:
            self.platform_review_status.setText("还差：音乐")
        elif not self._location_applied:
            self.platform_review_status.setText("还差：地点")
        elif not self._declaration_applied:
            self.platform_review_status.setText("还差：声明")
        else:
            self.platform_review_status.setText("检查设置")

    def _sync_location_view(self) -> None:
        """在搜索、候选确认和已回读之间切换，不依赖隐藏列表的选中态。"""

        selected_location = self._selected_location()
        if self._location_applied and selected_location:
            self.location_view_stack.setCurrentWidget(self.location_applied_view)
            self.location_applied_card.setText(self._location_display(selected_location))
            return
        if self._pending_location or selected_location:
            self.location_view_stack.setCurrentWidget(self.location_candidate_view)
            self.location_candidate_card.setText(
                f"{self._location_display(self._pending_location or selected_location)}\n正在写入平台并回读"
            )
            return
        self.location_view_stack.setCurrentWidget(self.location_search_view)

    def _step_complete(self, step: int) -> bool:
        if step == 0:
            if self.selected_video_count() >= 1:
                return bool(self._setup_generation_id)
            return bool(self._session_id) and self._content_change_kind() == "none"
        if step == 1:
            return self._can_review()
        if self.selected_video_count() >= 1:
            return self._can_batch_review()
        return self._can_review()

    def _busy(self) -> bool:
        return bool(self._immediate_write_kind) or any(
            self.runner.is_running(key)
            for key in (
                "douyin_commerce_upload",
                "douyin_commerce_sync_content",
                self._SETUP_GENERATION_TASK_KEY,
                self._SETUP_GENERATION_CLOSE_TASK_KEY,
                self._COLLECTOR_TASK_KEY,
                self._IMMEDIATE_WRITE_KEY,
                "douyin_commerce_preflight",
                "douyin_commerce_submit",
                _BATCH_RUN_KEY,
            )
        )

    def _content_is_valid(self) -> bool:
        if self.selected_video_count() >= 1:
            return self._batch_content_is_valid()
        return bool(
            self._selected_account()
            and self._selected_video()
            and self.description_input.toPlainText().strip()
        )

    def _can_review(self) -> bool:
        if self.timer_enabled.isChecked():
            try:
                self._schedule_text()
            except ValueError:
                return False
        return bool(
            self._session_id
            and self._content_change_kind() == "none"
            and self._selected_music is not None
            and self._location_applied
            and self._declaration_applied
            and bool(self._selected_declaration())
        )

    def _batch_content_is_valid(self) -> bool:
        return bool(
            self._selected_account()
            and 1 <= self.selected_video_count() <= 20
            and self.description_input.toPlainText().strip()
        )

    def _can_batch_review(self) -> bool:
        if not self._batch_content_is_valid() or not self._selected_music:
            return False
        if not self._selected_declaration():
            return False
        if self.batch_publish_mode.currentData() == "interval-schedule":
            try:
                _future_schedule_text(
                    self.batch_start_date.date(), self.batch_start_time.time()
                )
            except ValueError:
                return False
        return all(
            _normalized(self._batch_locations.get(_normalized(video.get("storedPath")), {}).get("address"))
            for video in self._selected_videos()
        )

    def _schedule_text(self) -> str:
        if not self.timer_enabled.isChecked():
            return ""
        return _future_schedule_text(self.schedule_date.date(), self.schedule_time.time())

    def _tags(self) -> list[str]:
        tags = list(self._tag_values)
        for tag in self._parse_tag_text(self.tags_input.text()):
            if tag not in tags:
                tags.append(tag)
        return tags

    def collect_batch_payload(self) -> dict:
        """收集批量工作台的白名单载荷；不含会话、验证码或浏览器数据。"""

        account = self._selected_account()
        videos = self._selected_videos()
        if not account or int(account.get("type") or 0) != 3 or int(account.get("status") or 0) != 1:
            raise douyin_commerce_batch_service.DouyinCommerceBatchError("请选择一个状态正常的抖音账号")
        if not 1 <= len(videos) <= 20:
            raise douyin_commerce_batch_service.DouyinCommerceBatchError("请选择 1 至 20 条视频")
        items = []
        for video in videos:
            path = _normalized(video.get("storedPath"))
            location = self._batch_locations.get(path)
            if not isinstance(location, dict):
                raise douyin_commerce_batch_service.DouyinCommerceBatchError("每条视频必须选择带完整地址的官方地点预设")
            items.append(
                {
                    "mediaPath": path,
                    "locationPreset": dict(location),
                    "scheduleTimeOverride": _normalized(self._batch_schedule_overrides.get(path)),
                }
            )
        publish_mode = _normalized(self.batch_publish_mode.currentData() or "immediate")
        schedule: dict[str, object] = {}
        if publish_mode == "interval-schedule":
            schedule = {
                "timezone": "Asia/Shanghai",
                "startTime": datetime(
                    self.batch_start_date.date().year(),
                    self.batch_start_date.date().month(),
                    self.batch_start_date.date().day(),
                    self.batch_start_time.time().hour(),
                    self.batch_start_time.time().minute(),
                ).strftime("%Y-%m-%d %H:%M"),
                "intervalMinutes": self.batch_interval_minutes.value(),
            }
        raw_batch = {
            "type": 3,
            "workflow": "douyin-commerce-batch",
            "commerceMode": "local-group-buy",
            "contentType": "video",
            "accountList": [str(account.get("filePath") or "")],
            "backgroundMode": self.background_mode_checkbox.isChecked(),
            "shared": {
                "title": self.title_input.text().strip(),
                "description": self.description_input.toPlainText().strip(),
                "tags": self._tags(),
                "selectedMusic": dict(self._selected_music or {}),
                "contentDeclaration": self._selected_declaration(),
            },
            "publishMode": publish_mode,
            "schedule": schedule,
            "items": items,
        }
        # 一次点击只读取一次北京时间，并用它同时生成任务明细和执行排期。
        # 后续预检会重新验证“仍为未来”，但不会丢掉本次显式逐条字段。
        return douyin_commerce_batch_service.prepare_batch_for_execution(
            raw_batch,
            now=datetime.now(_SHANGHAI_TZ).replace(second=0, microsecond=0),
        )

    def _batch_draft_payload(self) -> dict:
        account = self._selected_account() or {}
        location_search = self._batch_location_state()
        return {
            "accountId": account.get("id"),
            "accountFile": account.get("filePath"),
            "shared": {
                "title": self.title_input.text(),
                "description": self.description_input.toPlainText(),
                "tags": self._tags(),
                "selectedMusic": dict(self._selected_music or {}),
                "contentDeclaration": self._selected_declaration(),
            },
            # 只保存用户最后的搜索意图；平台候选必须在新会话中重新读取。
            "lastLocationSearch": {
                "scope": location_search["scope"],
                "keyword": location_search["keyword"],
                "commissionFilter": location_search["commissionFilter"],
            },
            "publishMode": _normalized(self.batch_publish_mode.currentData() or "immediate"),
            "schedule": {
                "timezone": "Asia/Shanghai",
                "startTime": datetime(
                    self.batch_start_date.date().year(),
                    self.batch_start_date.date().month(),
                    self.batch_start_date.date().day(),
                    self.batch_start_time.time().hour(),
                    self.batch_start_time.time().minute(),
                ).strftime("%Y-%m-%d %H:%M"),
                "intervalMinutes": int(self.batch_interval_minutes.value()),
            },
            "items": [
                {
                    "mediaPath": _normalized(video.get("storedPath")),
                    "locationPresetId": _normalized(self._batch_locations.get(_normalized(video.get("storedPath")), {}).get("id")),
                    "locationPreset": dict(
                        self._batch_locations.get(
                            _normalized(video.get("storedPath")),
                            {},
                        )
                    ),
                    "enableTimer": self.batch_publish_mode.currentData() == "interval-schedule",
                    "scheduleTimeOverride": _normalized(self._batch_schedule_overrides.get(_normalized(video.get("storedPath")))),
                }
                for video in self._selected_videos()
            ],
        }

    def save_batch_content(self) -> None:
        """保存批量内容；仅写本地草稿，成功后不弹窗打断下一步。"""

        try:
            saved = douyin_commerce_batch_draft_service.save_batch_draft(self._batch_draft_payload())
            self._remember_current_tags()
        except Exception as exc:
            QMessageBox.warning(self, "保存本地内容", str(exc))
            return
        self.reference_save_badge.setText(f"本地内容已保存 · {saved.get('updatedAt') or '刚刚'}")
        self._batch_saved_content_available = True
        self._sync_view()

    def restore_batch_content(self) -> None:
        """直接恢复本地草稿；仅缺失的账号/素材留在界面上提示，不弹成功提示。"""

        try:
            saved = douyin_commerce_batch_draft_service.load_batch_draft()
        except Exception as exc:
            QMessageBox.warning(self, "恢复已保存内容", str(exc))
            return
        if not saved:
            self._refresh_batch_saved_content_status()
            self._sync_view()
            return
        payload = dict(saved.get("payload") or {})
        self.account_combo.blockSignals(True)
        self._restore_saved_combo(
            self.account_combo,
            identity=payload.get("accountId"), path=payload.get("accountFile"),
        )
        self.account_combo.blockSignals(False)
        by_path = {
            _normalized(self.video_combo.itemData(index).get("storedPath")): index
            for index in range(1, self.video_combo.count())
            if isinstance(self.video_combo.itemData(index), dict)
        }
        indexes = [
            by_path[_normalized(item.get("mediaPath"))]
            for item in payload.get("items") or []
            if _normalized(item.get("mediaPath")) in by_path
        ]
        if indexes:
            self.select_video_indexes(indexes)
        shared = payload.get("shared") if isinstance(payload.get("shared"), dict) else {}
        self.title_input.setText(str(shared.get("title") or ""))
        self.description_input.setPlainText(str(shared.get("description") or ""))
        self._set_tags(shared.get("tags") or [])
        restored_music = shared.get("selectedMusic")
        self._selected_music = (
            dict(restored_music)
            if isinstance(restored_music, dict) and restored_music
            else None
        )
        self._pending_music = None
        self._discard_music_candidates_for_reload()
        declaration = _normalized(shared.get("contentDeclaration"))
        if declaration not in self.declaration_buttons:
            declaration = self._DEFAULT_CONTENT_DECLARATION
        self._set_selected_declaration(declaration)
        self._confirmed_declaration = declaration
        self._declaration_applied = False
        self._pending_declaration = ""
        location_search = (
            payload.get("lastLocationSearch")
            if isinstance(payload.get("lastLocationSearch"), dict)
            else {}
        )
        search_scope = _normalized(location_search.get("scope"))
        if search_scope not in {
            douyin_commerce_service.LOCATION_SCOPE_LOCAL,
            douyin_commerce_service.LOCATION_SCOPE_DOMESTIC,
        }:
            search_scope = douyin_commerce_service.LOCATION_SCOPE_DOMESTIC
        search_commission_filter = normalize_commission_filter(
            location_search.get("commissionFilter"),
            default=DEFAULT_COMMISSION_FILTER,
        )
        self._batch_location_searches = {
            _BATCH_SHARED_LOCATION_SEARCH_KEY: {
                "scope": search_scope,
                "keyword": _normalized(location_search.get("keyword")),
                "commissionFilter": search_commission_filter,
                "platformResultCount": 0,
                "rawCandidates": [],
                "candidates": [],
            }
        }
        self.batch_location_commission_combo.blockSignals(True)
        self.batch_location_commission_combo.setCurrentIndex(
            self.batch_location_commission_combo.findData(
                search_commission_filter
            )
        )
        self.batch_location_commission_combo.blockSignals(False)
        presets = {str(item.get("id")): item for item in self._current_location_presets()}
        self._batch_locations = {}
        self._batch_schedule_overrides = {}
        for item in payload.get("items") or []:
            path = _normalized(item.get("mediaPath"))
            snapshot = item.get("locationPreset")
            preset = (
                dict(snapshot)
                if isinstance(snapshot, dict)
                and _normalized(snapshot.get("poiId"))
                and _normalized(snapshot.get("name"))
                and _normalized(snapshot.get("address"))
                else presets.get(_normalized(item.get("locationPresetId")))
            )
            if preset:
                self._batch_locations[path] = dict(preset)
            if _normalized(item.get("scheduleTimeOverride")):
                self._batch_schedule_overrides[path] = _normalized(item.get("scheduleTimeOverride"))
        publish_mode = _normalized(payload.get("publishMode") or "")
        if publish_mode not in {"immediate", "interval-schedule"}:
            publish_mode = (
                "interval-schedule"
                if any(item.get("enableTimer") is True for item in payload.get("items") or [])
                else "immediate"
            )
        schedule = payload.get("schedule") if isinstance(payload.get("schedule"), dict) else {}
        start_time = _normalized(schedule.get("startTime"))
        if start_time:
            try:
                parsed = datetime.strptime(start_time, "%Y-%m-%d %H:%M")
                now = datetime.now(_SHANGHAI_TZ)
                # 草稿只保留更远的未来排期；今天或过去的旧日期
                # 不得覆盖“北京时间次日 16:00”的当前默认规则。
                if parsed.date() <= now.date():
                    parsed = self._default_schedule_datetime(now)
                self.batch_start_date.setDate(QDate(parsed.year, parsed.month, parsed.day))
                self.batch_start_time.setTime(QTime(parsed.hour, parsed.minute))
            except ValueError:
                pass
        try:
            interval = int(schedule.get("intervalMinutes") or 0)
        except (TypeError, ValueError):
            interval = 0
        if interval > 0:
            self.batch_interval_minutes.setValue(interval)
        self.batch_publish_mode.setCurrentIndex(
            self.batch_publish_mode.findData(publish_mode)
        )
        self._refresh_batch_saved_content_status()
        self._render_batch_item_rows()
        self._sync_view()

    def start_batch_preflight(self) -> None:
        """在后台逐条执行批量 dry-run；不会保存草稿或最终发表。"""

        try:
            payload = self.collect_batch_payload()
        except Exception as exc:
            QMessageBox.warning(self, "批量预检", str(exc))
            return
        task = task_service.create_douyin_batch_task(payload, mode="oneclick_preflight")
        self._batch_task_id = int(task["id"])
        task_id = self._batch_task_id
        self._batch_pause_requested = False
        self._batch_editor_session_ended = False
        self.validation_label.setText(f"正在检查 0/{len(payload['items'])} 条视频…")

        def run_preflight(report) -> object:
            def execute() -> object:
                task_service.mark_task_running(
                    task_id, "抖音带货批量开始逐条发布前检查"
                )
                return self._batch_executor.run_preflight(
                    payload,
                    task_id=task_id,
                    progress=lambda event: report(event.to_public_dict()),
                )

            return self._run_after_collector_barrier(
                execute,
                reason="preflight_started",
            )

        self.runner.run(
            _BATCH_RUN_KEY,
            with_progress=run_preflight,
            on_progress=self._batch_progress,
            on_success=lambda result: self._batch_preflight_succeeded(payload, result),
            on_error=self._batch_operation_failed,
            on_finished=self._batch_operation_finished,
        )
        self._sync_view()

    def _batch_progress(self, event: object) -> None:
        if not isinstance(event, dict):
            return
        index = int(event.get("index") or 0) + 1
        total = int(event.get("total") or self.selected_video_count())
        phase = _normalized(event.get("phase"))
        remaining_seconds = event.get("remainingSeconds")
        if phase == "verification_cooldown":
            if type(remaining_seconds) is not int or remaining_seconds < 0:
                return
            self._batch_progress_text = (
                f"短信验证码冷却中，剩余 {remaining_seconds} 秒；"
                f"到点自动继续第 {index}/{total} 条"
            )
            self.validation_label.setText(self._batch_progress_text)
            return
        completed, succeeded, failed = self._batch_execution_counts(total)
        self._batch_progress_text = (
            f"已完成 {completed}/{total} · 成功 {succeeded} · 失败 {failed}"
            f" · 正在处理 {index}/{total}：{_normalized(event.get('message'))}"
        )
        self.validation_label.setText(self._batch_progress_text)
        _LOGGER.info("抖音带货批量执行：%s", self._batch_progress_text)
        if phase == "waiting_verification":
            videos = self._selected_videos()
            label = Path(_normalized(videos[index - 1].get("storedPath"))).name if 0 < index <= len(videos) else ""
            self._verification_item_context = {"index": index, "total": total, "label": label}

    def _batch_execution_counts(self, total: int) -> tuple[int, int, int]:
        """从当前任务记录读取逐条结果，只用于底部实时进度摘要。"""

        task_id = int(self._batch_task_id or 0)
        if task_id <= 0:
            return 0, 0, 0
        try:
            task = task_service.get_task(task_id)
            rows = task.get("items") if isinstance(task, dict) else []
        except Exception:
            return 0, 0, 0
        statuses = [_normalized(row.get("status")) for row in rows if isinstance(row, dict)]
        succeeded = sum(status == "success" for status in statuses)
        failed = sum(status == "failed" for status in statuses)
        completed = min(max(0, int(total)), succeeded + failed)
        return completed, succeeded, failed

    def _batch_preflight_succeeded(self, payload: dict, result: object) -> None:
        rows = result if isinstance(result, list) else []
        if len(rows) != len(payload.get("items") or []) or any(row.get("status") != "preflighted" for row in rows if isinstance(row, dict)):
            self._batch_operation_failed("批量预检未取得全部逐条回读")
            return
        self._batch_preflight_fingerprint = self._batch_fingerprint(payload)
        self._batch_editor_session_ended = True
        self.validation_label.setText("全部视频已完成发布前检查；尚未提交。")

    def _batch_operation_failed(self, message: str) -> None:
        self._batch_preflight_fingerprint = ""
        diagnostic = _normalized(message) or "未知异常"
        self._batch_result_feedback = f"批量任务未完成：{diagnostic}"
        self.validation_label.setText(self._batch_result_feedback)
        _LOGGER.warning("抖音带货批量操作未完成: %s", diagnostic)
        QMessageBox.warning(self, "抖音带货批量提交失败", self._batch_result_feedback)

    def _batch_operation_finished(self) -> None:
        """批量提交结束后收束验证内存与原生对话框。"""

        self._cleanup_douyin_verification()
        self._batch_task_id = None
        self._batch_pause_requested = False
        self._sync_view()

    @staticmethod
    def _batch_fingerprint(payload: dict) -> str:
        import json
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def open_batch_submit_confirmation(self) -> None:
        """点击确认提交后直接启动批量发布，不再重复弹出二次确认。"""

        try:
            payload = self.collect_batch_payload()
        except Exception as exc:
            QMessageBox.warning(self, "确认批量提交", str(exc))
            return
        task = task_service.create_douyin_batch_task(payload, mode="oneclick_publish")
        self.start_batch_publish(payload, task)

    def open_batch_resume(self, task_id: int) -> None:
        """确认前只读取资格；用户确认后才创建子任务并进入既有执行链路。"""

        resume_now = douyin_commerce_batch_service.current_shanghai_time()
        plan = task_service.prepare_douyin_batch_resume(int(task_id), now=resume_now)
        if plan.get("resumeAllowed") is not True:
            QMessageBox.warning(self, "继续发布", str(plan.get("blockedReason") or "当前任务不可继续发布"))
            return
        dialog = DouyinCommerceBatchResumeConfirmDialog(plan, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if self.runner.is_running(_BATCH_RUN_KEY):
            QMessageBox.warning(
                self,
                "继续发布",
                "当前已有批量发布任务正在运行，请等待结束后再继续。",
            )
            return
        try:
            created = task_service.create_douyin_batch_resume(int(task_id), now=resume_now)
        except Exception as exc:
            QMessageBox.warning(self, "继续发布", str(exc))
            return
        self.start_batch_publish(dict(created["batch"]), dict(created["task"]))

    def start_batch_publish(self, payload: dict, task: dict) -> None:
        """确认后启动批量最终提交；只由平台逐条回执决定已发布状态。"""

        if self.runner.is_running(_BATCH_RUN_KEY):
            QMessageBox.warning(
                self,
                "批量发布",
                "当前已有批量发布任务正在运行，请等待结束后再继续。",
            )
            return
        try:
            self._batch_executor.reset_shutdown()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            QMessageBox.warning(
                self,
                "批量发布",
                "批量发布执行器未能启动，已安全停止",
            )
            return

        self._batch_task_id = int(task["id"])
        task_id = self._batch_task_id
        self._batch_pause_requested = False
        self._batch_preflight_fingerprint = ""
        self._batch_result_feedback = ""
        total = len(payload.get("items") or [])
        self._batch_progress_text = f"已完成 0/{total} · 成功 0 · 失败 0 · 正在启动…"
        self.validation_label.setText(self._batch_progress_text)
        _LOGGER.info("抖音带货批量执行：%s", self._batch_progress_text)

        def run_publish(report) -> object:
            def execute() -> object:
                task_service.mark_task_running(
                    task_id, "抖音带货批量开始最终提交"
                )
                return self._batch_executor.run_publish(
                    payload,
                    task_id=task_id,
                    confirmed=True,
                    progress=lambda event: report(event.to_public_dict()),
                )

            return self._run_after_collector_barrier(
                execute,
                reason="publish_started",
            )

        started = self.runner.run(
            _BATCH_RUN_KEY,
            with_progress=run_publish,
            on_progress=self._batch_progress,
            on_success=self._batch_publish_succeeded,
            on_error=self._batch_operation_failed,
            on_finished=self._batch_operation_finished,
        )
        if not started:
            self._batch_operation_failed("提交任务未能启动，可能已有任务正在运行")
        else:
            self._start_douyin_verification_polling()
        self._sync_view()

    def _batch_publish_succeeded(self, result: object) -> None:
        """无论逐条结果成功或失败，都给出可见、可追溯的最终汇总。"""

        rows = [dict(row) for row in result if isinstance(row, dict)] if isinstance(result, list) else []
        if rows:
            # 结果页仍要保留本批日志供复制和排障；等用户真正进入下一批平台
            # 设置时再清空，直接重试当前批次不会丢失上下文。
            self._clear_runtime_log_on_next_task = True
        published = [row for row in rows if _normalized(row.get("status")) == "published"]
        failed = [
            row
            for row in rows
            if _normalized(row.get("status")) in {"failed", "verification_failed"}
        ]
        waiting = [
            row
            for row in rows
            if _normalized(row.get("status")) in {"waiting_login", "waiting_verification"}
        ]
        ambiguous = [
            row
            for row in rows
            if _normalized(row.get("status")) == "receipt_ambiguous"
        ]
        pending = [row for row in rows if _normalized(row.get("status")) == "pending"]
        paused = [row for row in rows if _normalized(row.get("status")) == "paused"]
        if paused or ambiguous:
            parts = [f"批量提交已暂停：成功 {len(published)} 条，失败 {len(failed)} 条"]
        else:
            parts = [f"批量提交已结束：成功 {len(published)} 条，失败 {len(failed)} 条"]
        if waiting:
            parts.append(f"待人工处理 {len(waiting)} 条")
        if ambiguous:
            parts.append(f"平台状态待核对 {len(ambiguous)} 条")
        if pending:
            parts.append(f"未开始 {len(pending)} 条")
        if paused:
            parts.append(f"已暂停 {len(paused)} 条")
        diagnostics = [
            _normalized(row.get("diagnostic"))
            for row in failed
            if _normalized(row.get("diagnostic"))
        ]
        if diagnostics:
            parts.append(f"失败原因：{diagnostics[0]}")
        ambiguous_diagnostics = [
            _normalized(row.get("diagnostic"))
            for row in ambiguous
            if _normalized(row.get("diagnostic"))
        ]
        if ambiguous_diagnostics:
            parts.append(f"待核对原因：{ambiguous_diagnostics[0]}")
        self._batch_result_feedback = "；".join(parts) + "。"
        self.validation_label.setText(self._batch_result_feedback)
        _LOGGER.info("抖音带货批量结果：%s", self._batch_result_feedback)
        if rows and len(published) == len(rows):
            if self._setup_generation_id:
                self._dispatch_setup_generation_close(
                    reason="publish_completed",
                    completion="publish_completed",
                    silent=True,
                )
            else:
                self._clear_current_batch_platform_choices()
        if failed or waiting or paused or ambiguous:
            QMessageBox.warning(self, "抖音带货批量提交结果", self._batch_result_feedback)
        else:
            QMessageBox.information(self, "抖音带货批量提交成功", self._batch_result_feedback)

    def _batch_summary_rows(self, payload: dict) -> list[dict[str, str]]:
        scheduled = douyin_commerce_batch_service.apply_interval_schedule(
            payload, now=datetime.now(_SHANGHAI_TZ)
        )
        return [
            {
                "index": str(index),
                "video": Path(str(item.get("mediaPath") or "")).name,
                "location": f"{item.get('locationPreset', {}).get('name', '')} · {item.get('locationPreset', {}).get('address', '')}",
                "schedule": f"北京时间 {item.get('scheduleTime')}" if item.get("enableTimer") else "立即发布",
            }
            for index, item in enumerate(scheduled.get("items") or [], start=1)
        ]

    def collect_upload_payload(self) -> dict:
        account = self._selected_account()
        video = self._selected_video()
        if not account:
            raise douyin_commerce_service.DouyinCommerceError("请选择一个状态正常的抖音账号")
        if int(account.get("type") or 0) != 3 or int(account.get("status") or 0) != 1:
            raise douyin_commerce_service.DouyinCommerceError("所选账号不是状态正常的抖音账号")
        if not video:
            raise douyin_commerce_service.DouyinCommerceError("请选择一条视频素材")
        payload = {
            "type": 3,
            "workflow": douyin_commerce_service.DOUYIN_COMMERCE_WORKFLOW,
            "commerceMode": douyin_commerce_service.LOCAL_GROUP_BUY_MODE,
            "contentType": "video",
            "accountId": account.get("id"),
            "title": self.title_input.text().strip(),
            "description": self.description_input.toPlainText().strip(),
            "tags": self._tags(),
            "fileList": [str(video.get("storedPath") or "")],
            "musicMode": "favorite-manual",
            "accountList": [str(account.get("filePath") or "")],
            "enableTimer": False,
            "runtimeMode": "preflight",
            "debugDryRun": True,
            "backgroundMode": self.background_mode_checkbox.isChecked(),
            "syncToToutiao": False,
        }
        return douyin_commerce_service.validate_douyin_commerce_upload_payload(payload)

    def collect_payload(self, runtime_mode: str) -> dict:
        base = self.collect_upload_payload()
        location = self._selected_location()
        timer_enabled = self.timer_enabled.isChecked()
        payload = {
            **base,
            "locationKeyword": str((location or {}).get("name") or ""),
            "locationPoi": dict(location or {}),
            "locationScope": self._selected_location_scope(),
            "contentDeclaration": self._selected_declaration(),
            "selectedMusic": dict(self._selected_music or {}),
            "enableTimer": timer_enabled,
            "scheduleTime": self._schedule_text() if timer_enabled else "",
            "runtimeMode": runtime_mode,
            "debugDryRun": runtime_mode == "preflight",
            "backgroundMode": self.background_mode_checkbox.isChecked(),
        }
        return douyin_commerce_service.validate_douyin_commerce_payload(payload)

    def _payload_fingerprint(self, runtime_mode: str) -> str:
        try:
            payload = self.collect_payload(runtime_mode)
        except Exception:
            return ""
        music = payload.get("selectedMusic") or {}
        poi = payload.get("locationPoi") or {}
        return "|".join(
            (
                str(payload["accountList"][0]),
                str(payload["fileList"][0]),
                str(payload["title"]),
                str(payload["description"]),
                ",".join(str(tag) for tag in payload.get("tags") or []),
                str(music.get("musicId") or ""),
                str(poi.get("poiId") or ""),
                str(payload.get("locationScope") or ""),
                str(payload.get("contentDeclaration") or ""),
                str(payload["scheduleTime"]),
            )
        )

    def _summary(self) -> dict[str, str]:
        account = self._selected_account() or {}
        video = self._selected_video() or {}
        location = self._selected_location() or {}
        account_name, subject = _account_identity(account)
        account_summary = account_name or "待选择"
        if account_name:
            account_summary = f"{account_name}\n主体：{subject or '未设置主体'}"
        video_metadata = media_service.video_display_metadata(video) if video else {}
        video_summary = self._video_display(video, video_metadata)
        if self.timer_enabled.isChecked():
            try:
                schedule = f"北京时间 {self._schedule_text()}"
            except ValueError as exc:
                schedule = str(exc)
        else:
            schedule = "立即发表（最终仍需单独确认）"
        description = self.description_input.toPlainText().strip()
        if len(description) > 120:
            description = f"{description[:120].rstrip()}…"
        return {
            "account": account_summary,
            "video": video_summary,
            "title": self.title_input.text().strip() or "待填写",
            "description": description or "待填写",
            "music": self._music_display(self._selected_music) if self._selected_music else "待由用户选择",
            "location": self._location_display(location) if self._location_applied and location else "待写入编辑页",
            "declaration": (
                self._selected_declaration()
                if self._declaration_applied and self._selected_declaration()
                else "待选择并回读"
            ),
            "schedule": schedule,
        }

    def _render_batch_review_rows(self) -> None:
        if not hasattr(self, "batch_review_rows_layout"):
            return
        while self.batch_review_rows_layout.count():
            item = self.batch_review_rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        is_batch = self.selected_video_count() >= 1
        self.batch_review_rows.setVisible(is_batch)
        self.copy_batch_publish_info_button.setEnabled(is_batch)
        self.summary_grid.setEnabled(True)
        if not is_batch:
            return
        for index, video in enumerate(self._selected_videos(), start=1):
            path = _normalized(video.get("storedPath"))
            location = self._batch_locations.get(path) or {}
            card = QFrame()
            card.setObjectName("douyinCommerceBatchReviewRow")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(12, 9, 12, 9)
            card_layout.setSpacing(3)
            video_label = QLabel(
                f"视频名称：{self._batch_review_video_name(video, path)}"
            )
            location_label = QLabel(
                f"地点：{location.get('name') or '待选择'} · {location.get('address') or ''}"
            )
            schedule = self.item_schedule_text(index - 1)
            schedule_label = QLabel(
                "发布时间：直接发布"
                if schedule == "立即发布"
                else f"发布时间：定时：{schedule}"
            )
            for label in (video_label, location_label, schedule_label):
                label.setWordWrap(True)
                label.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextSelectableByMouse
                )
                label.setSizePolicy(
                    QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
                )
                card_layout.addWidget(label)
            self.batch_review_rows_layout.addWidget(card)
        self.batch_review_rows_layout.addStretch(1)

    @staticmethod
    def _batch_review_video_name(video: dict, path: str) -> str:
        """展示原始视频名，并去除素材库保存时附加的 UUID 前缀。"""

        name = _normalized(video.get("filename")) or Path(path).name
        return re.sub(
            r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}_",
            "",
            name,
            flags=re.IGNORECASE,
        )

    def copy_batch_publish_information(self) -> None:
        """复制当前批量的逐条发布信息，方便用户留存或核对。"""

        lines: list[str] = []
        for index, video in enumerate(self._selected_videos()):
            path = _normalized(video.get("storedPath"))
            location = self._batch_locations.get(path) or {}
            schedule = self.item_schedule_text(index)
            lines.extend(
                (
                    f"视频名称：{self._batch_review_video_name(video, path)}",
                    f"地点：{location.get('name') or '待选择'} · {location.get('address') or ''}",
                    "发布时间：直接发布"
                    if schedule == "立即发布"
                    else f"发布时间：定时：{schedule}",
                    "",
                )
            )
        content = "\n".join(lines).strip()
        if not content:
            self.copy_batch_publish_info_button.setText("暂无发布信息")
            return
        QApplication.clipboard().setText(content)
        self.copy_batch_publish_info_button.setText("已复制")

    def _copy_collector_diagnostics(self) -> None:
        """复制当前代际的公开诊断摘要，不弹出阻塞对话框。"""

        generation_id = _normalized(self._setup_generation_id)
        events: list[dict[str, object]] = []
        if generation_id:
            try:
                public_events = (
                    douyin_commerce_collectors.commerce_collector_manager
                    .recent_diagnostics(generation_id)
                )
            except Exception:
                public_events = []
            if isinstance(public_events, list):
                events = [
                    dict(event)
                    for event in public_events
                    if isinstance(event, Mapping)
                    and _normalized(event.get("setupGenerationId")) == generation_id
                ]

        collector_copy = {
            "domestic_location": "国内地点",
            "favorite_music": "收藏音乐",
            "local_location": "本地点",
        }
        allowed_actions = {
            "begin_generation",
            "refresh_favorite_music",
            "search_locations",
            "retry_collector",
            "close_generation",
        }
        allowed_cleanup = {
            "closed",
            "not_started",
            "cleanup_incomplete",
            "cleanup_interrupted",
        }
        lines: list[str] = []
        for event in sorted(
            events,
            key=lambda item: _normalized(item.get("timestamp")),
        ):
            collector_type = _normalized(event.get("collectorType"))
            collector_name = collector_copy.get(collector_type, "未知采集器")
            action = _normalized(event.get("action"))
            if action not in allowed_actions:
                action = "collector_action"
            raw_keyword = event.get("keyword")
            keyword = (
                douyin_commerce_collectors._redact_diagnostic_text(
                    raw_keyword, limit=80
                )
                if type(raw_keyword) is str and raw_keyword
                else ""
            )
            candidate_count = event.get("candidateCount")
            if type(candidate_count) is not int or candidate_count < 0:
                candidate_count = 0
            duration_ms = event.get("durationMs")
            if type(duration_ms) is not int or duration_ms < 0:
                duration_ms = 0
            raw_error_code = _normalized(event.get("errorCode"))
            error_code = (
                "ok"
                if _normalized(event.get("outcome")) == "success"
                else self._public_collector_error_code(raw_error_code)
            )
            cleanup_result = _normalized(event.get("cleanupResult"))
            if cleanup_result not in allowed_cleanup:
                cleanup_result = "-"
            timestamp = self._collector_diagnostic_time(event.get("timestamp"))
            lines.append(
                f"{timestamp} | 批次 {generation_id[:6]} | {collector_name} | "
                f"{action} | 关键词={keyword} | 候选={candidate_count} | "
                f"{duration_ms}ms | {error_code} | cleanup={cleanup_result}"
            )

        QApplication.clipboard().setText(
            "\n".join(lines) if lines else "当前批次暂无采集诊断"
        )
        self.copy_collector_diagnostics_button.setText("诊断已复制")
        QTimer.singleShot(1500, self._restore_collector_diagnostics_button)

    @staticmethod
    def _collector_diagnostic_time(value: object) -> str:
        raw = _normalized(value)
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(_SHANGHAI_TZ)
            return parsed.strftime("%H:%M:%S")
        except (TypeError, ValueError):
            return "--:--:--"

    def _restore_collector_diagnostics_button(self) -> None:
        try:
            self.copy_collector_diagnostics_button.setText("复制诊断摘要")
        except RuntimeError:
            # 页面已销毁时，延迟回调无需再更新按钮。
            return

    @staticmethod
    def _public_collector_error_code(value: object) -> str:
        """只允许固定错误码进入 UI，底层异常全文仅留本机日志。"""

        code = _normalized(value)
        return code if code in COLLECTOR_ERROR_COPY else "collector_unknown"

    @staticmethod
    def _collector_detail(status: Mapping[str, Any], collector_type: str) -> dict[str, Any]:
        collectors = status.get("collectors")
        raw_state = collectors.get(collector_type) if isinstance(collectors, Mapping) else None
        detail: dict[str, Any] = (
            dict(raw_state) if isinstance(raw_state, Mapping) else {"state": raw_state}
        )
        details = status.get("collectorDetails")
        if isinstance(details, Mapping) and isinstance(details.get(collector_type), Mapping):
            detail.update(dict(details[collector_type]))
        instances = status.get("collectorInstanceIds")
        if isinstance(instances, Mapping) and not detail.get("instanceId"):
            detail["instanceId"] = instances.get(collector_type)
        return detail

    def _render_collector_status(self, status: Mapping[str, Any]) -> None:
        """投影三采集器公开状态，不显示代际、实例或底层会话标识。"""

        if not isinstance(status, Mapping):
            return
        generation_id = _normalized(status.get("setupGenerationId"))
        if self._setup_generation_id and generation_id != self._setup_generation_id:
            return
        if not generation_id:
            return
        self._collector_status = dict(status)
        labels = {
            "domestic_location": (self.domestic_collector_status, "国内地点"),
            "favorite_music": (self.music_collector_status, "收藏音乐"),
            "local_location": (self.local_collector_status, "本地点"),
        }
        defaults = {
            "domestic_location": "等待进入设置",
            "favorite_music": "点击刷新后启动",
            "local_location": "首次搜索时启动",
        }
        state_copy = {
            "starting": "启动中",
            "active": "可用",
            "retrying": "重试中",
            "closing": "正在关闭",
            "closed": "已关闭",
        }
        failed_types: list[str] = []
        for collector_type, (label, title) in labels.items():
            detail = self._collector_detail(status, collector_type)
            state = _normalized(detail.get("state")) or "not_started"
            if state == "not_started":
                body = defaults[collector_type]
            elif state == "failed":
                error_code = self._public_collector_error_code(detail.get("errorCode"))
                body = (
                    f"失败 · {COLLECTOR_ERROR_COPY[error_code]} · "
                    f"错误码 {error_code}"
                )
                failed_types.append(collector_type)
            else:
                body = state_copy.get(state, "状态未知")
            candidate_count = detail.get("candidateCount")
            duration_ms = detail.get("durationMs")
            suffixes: list[str] = []
            if type(candidate_count) is int and candidate_count >= 0:
                suffixes.append(f"候选 {candidate_count}")
            if type(duration_ms) is int and duration_ms >= 0:
                suffixes.append(f"耗时 {duration_ms}ms")
            if suffixes:
                body = f"{body} · " + " · ".join(suffixes)
            label.setText(f"{title}：{body}")

        # 单槽重试：同一时刻只暴露一个明确失败项，不批量重启其他采集器。
        if failed_types:
            preferred = self._last_failed_collector_type
            self._last_failed_collector_type = (
                preferred if preferred in failed_types else failed_types[0]
            )
            retry_copy = {
                "domestic_location": "重试国内地点",
                "favorite_music": "重试收藏音乐",
                "local_location": "重试本地点",
            }
            self.retry_collector_button.setText(
                retry_copy[self._last_failed_collector_type]
            )
            self.retry_collector_button.setVisible(True)
            self.retry_collector_button.setEnabled(not self._busy())
        else:
            self._last_failed_collector_type = ""
            self.retry_collector_button.setVisible(False)

    def _clear_runtime_log_for_new_task_if_needed(self) -> None:
        """在上一批已结束且下一批真正开始时建立新的日志段。"""

        if not self._clear_runtime_log_on_next_task:
            return
        bus = runtime_log_bus()
        bus.clear()
        self._clear_runtime_log_on_next_task = False
        bus.publish("新的抖音带货任务已开始，上一批客户端执行日志已清空")

    def _start_setup_generation(self, payload: dict) -> None:
        """关闭旧代际并建立本次平台设置代际；不创建正式发布会话。"""

        if self._shutdown_requested.is_set() or self.runner.is_running(
            self._SETUP_GENERATION_TASK_KEY
        ):
            return
        self._clear_runtime_log_for_new_task_if_needed()
        old_generation_id = self._setup_generation_id
        self._setup_start_token += 1
        start_token = self._setup_start_token
        self._reset_platform_collector_progress()
        self._start_platform_collector_progress(
            "",
            "domestic_location",
            start_token,
            "正在准备国内地点采集",
        )
        self._collector_status = {}
        self._last_failed_collector_type = ""
        self._setup_start_error_code = ""
        self._setup_close_error_code = ""
        self.retry_collector_button.setVisible(False)
        self.platform_review_status.setText("正在准备平台设置采集器…")

        def begin(report):
            def operation() -> object:
                if self._shutdown_requested.is_set():
                    raise RuntimeError("client_shutdown")
                # 内部 runtime 可能在公开 ID 返回前失败；先登记清理义务，
                # 让异常回调仍能通过 close_generation(None) 统一收口。
                self._setup_generation_cleanup_required = True
                if self._shutdown_requested.is_set():
                    raise RuntimeError("client_shutdown")
                result = douyin_commerce_collectors.commerce_collector_manager.begin_generation(
                    dict(payload), on_progress=report
                )
                if self._shutdown_requested.is_set():
                    generation_id = (
                        _normalized(result.get("setupGenerationId"))
                        if isinstance(result, Mapping)
                        else ""
                    )
                    try:
                        close_result = (
                            douyin_commerce_collectors.commerce_collector_manager.close_generation(
                                generation_id or None,
                                reason="client_shutdown",
                            )
                        )
                    except Exception:
                        close_result = {
                            "closed": False,
                            "aliveCollectorCount": 1,
                        }
                    if self._collector_close_is_complete(close_result):
                        self._setup_generation_cleanup_required = False
                    elif generation_id:
                        self._setup_generation_id = generation_id
                    raise RuntimeError("client_shutdown")
                return result

            if old_generation_id:
                return self._run_after_collector_barrier(
                    operation,
                    reason="generation_replaced",
                )
            return operation()

        started = self.runner.run(
            self._SETUP_GENERATION_TASK_KEY,
            with_progress=begin,
            on_progress=self._set_commerce_progress,
            on_success=lambda result: self._accept_setup_generation_result(
                start_token, payload, result
            ),
            on_error=lambda message: self._setup_generation_failed(start_token, message),
            on_finished=lambda: self._setup_generation_finished(start_token),
        )
        if not started:
            self._setup_generation_failed(start_token, "collector_start_failed")
            self._setup_generation_finished(start_token)

    def _setup_generation_finished(self, start_token: int) -> None:
        self._finish_platform_collector_progress(
            "",
            "domestic_location",
            start_token,
        )
        self._sync_view()

    def _accept_setup_generation_result(
        self, start_token: int, payload: Mapping[str, Any], result: object
    ) -> None:
        if self._shutdown_requested.is_set() or start_token != self._setup_start_token:
            return
        self._setup_generation_succeeded(result)
        result_generation_id = (
            _normalized(result.get("setupGenerationId"))
            if isinstance(result, Mapping)
            else ""
        )
        if result_generation_id and result_generation_id == self._setup_generation_id:
            self._uploaded_editor_payload = dict(payload)

    def _setup_generation_succeeded(self, result: object) -> None:
        """仅接纳当前、非空且国内采集器已就绪的代际结果。"""

        if not isinstance(result, Mapping):
            return
        generation_id = _normalized(result.get("setupGenerationId"))
        if _normalized(result.get("status")) == "needs_login" or _normalized(
            result.get("generationState")
        ) == "paused_for_login":
            if generation_id:
                self._setup_generation_id = generation_id
            self._handle_login_required()
            return
        if not generation_id:
            self._setup_generation_failed(self._setup_start_token, "collector_start_failed")
            return
        if self._setup_generation_id and generation_id != self._setup_generation_id:
            return
        domestic = self._collector_detail(result, "domestic_location")
        if _normalized(domestic.get("state")) != "active":
            self._setup_generation_id = generation_id
            self._setup_generation_failed(self._setup_start_token, "collector_start_failed")
            return
        self._clear_commerce_progress()
        self._setup_generation_id = generation_id
        self._session_id = ""
        self._render_collector_status(result)
        self._setup_start_error_code = ""
        self._clear_stage_error("content")
        self.content_notice.setVisible(False)
        self._go_to_step(1)

    def _setup_generation_failed(self, start_token: int, message: object) -> None:
        if self._shutdown_requested.is_set() or start_token != self._setup_start_token:
            return
        self._clear_commerce_progress()
        code = (
            "cleanup_incomplete"
            if _normalized(message) == self._COLLECTOR_BARRIER_ERROR
            else self._public_collector_error_code(message)
        )
        if code == "login_required":
            self._handle_login_required()
            return
        self._setup_start_error_code = code
        self.platform_review_status.setText(f"平台设置采集器未就绪 · 错误码 {code}")
        self._set_stage_error("content", code)
        self._sync_view()
        if code != "cleanup_incomplete" and (
            self._setup_generation_cleanup_required or self._setup_generation_id
        ):
            self._dispatch_setup_generation_close(
                reason="operation_failed",
                completion="operation_failed",
                silent=True,
            )

    @staticmethod
    def _collector_close_is_complete(result: object) -> bool:
        if not isinstance(result, Mapping) or result.get("closed") is not True:
            return False
        alive_count = result.get("aliveCollectorCount")
        return type(alive_count) is int and alive_count == 0

    def _close_setup_generation(self, reason: str) -> dict[str, object]:
        """同步执行统一 cancel-and-close；调用方负责将它放入后台任务。"""

        generation_id = self._setup_generation_id
        setup_starting = self.runner.is_running(self._SETUP_GENERATION_TASK_KEY)
        if (
            not generation_id
            and not setup_starting
            and not self._setup_generation_cleanup_required
        ):
            return {
                "closed": True,
                "setupGenerationId": "",
                "aliveCollectorCount": 0,
            }
        try:
            raw_result = (
                douyin_commerce_collectors.commerce_collector_manager.close_generation(
                    generation_id or None,
                    reason=reason,
                )
            )
        except Exception:
            return {
                "closed": False,
                "setupGenerationId": generation_id,
                "aliveCollectorCount": 1,
            }
        result = dict(raw_result) if isinstance(raw_result, Mapping) else {
            "closed": False,
            "setupGenerationId": generation_id,
            "aliveCollectorCount": 1,
        }
        if self._collector_close_is_complete(result):
            self._setup_generation_cleanup_required = False
            if not generation_id or self._setup_generation_id == generation_id:
                self._setup_generation_id = ""
        elif not self._setup_generation_id:
            returned_generation_id = _normalized(result.get("setupGenerationId"))
            if returned_generation_id:
                self._setup_generation_id = returned_generation_id
        return result

    def _run_after_collector_barrier(
        self,
        operation: Callable[[], object],
        *,
        reason: str,
    ) -> object:
        """只在协调器明确回读零存活采集器后执行后续操作。"""

        result = self._close_setup_generation(reason)
        if not self._collector_close_is_complete(result):
            raise RuntimeError(self._COLLECTOR_BARRIER_ERROR)
        return operation()

    def _dispatch_setup_generation_close(
        self,
        *,
        reason: str,
        completion: str,
        silent: bool,
    ) -> bool:
        """从 Qt 线程只派发关闭，不在界面回调中等待浏览器。"""

        has_generation = bool(
            self._setup_generation_id or self._setup_generation_cleanup_required
        )
        setup_starting = self.runner.is_running(self._SETUP_GENERATION_TASK_KEY)
        if not has_generation and not setup_starting:
            self._finish_setup_generation_close(completion, silent)
            return True
        if self.runner.is_running(self._SETUP_GENERATION_CLOSE_TASK_KEY):
            return False
        self._setup_close_token += 1
        close_token = self._setup_close_token
        self.platform_review_status.setText("正在关闭平台设置采集器…")
        started = self.runner.run(
            self._SETUP_GENERATION_CLOSE_TASK_KEY,
            with_progress=lambda _report: self._close_setup_generation(reason),
            on_success=lambda result: self._setup_generation_close_succeeded(
                close_token,
                completion,
                silent,
                result,
            ),
            on_error=lambda _message: self._setup_generation_close_failed(close_token),
            on_finished=self._sync_view,
        )
        if not started:
            self._setup_generation_close_failed(close_token)
        return started

    def _setup_generation_close_succeeded(
        self,
        close_token: int,
        completion: str,
        silent: bool,
        result: object,
    ) -> None:
        if close_token != self._setup_close_token:
            return
        if not self._collector_close_is_complete(result):
            self._setup_generation_close_failed(close_token)
            return
        self._reset_platform_collector_progress()
        self._collector_status = {}
        self._last_failed_collector_type = ""
        self._setup_close_error_code = ""
        self.retry_collector_button.setVisible(False)
        if completion != "operation_failed":
            self.platform_review_status.setText("平台设置采集器已关闭")
        self._finish_setup_generation_close(completion, silent)
        if completion == "operation_failed":
            self._setup_start_error_code = "collector_start_failed"
        self._sync_view()

    def _finish_setup_generation_close(self, completion: str, silent: bool) -> None:
        if completion in {"abandoned", "login_required", "operation_failed"}:
            self._reset_platform_settings_after_abandon()
            self._uploaded_editor_payload = None
            self._pending_upload_payload = None
            self._refresh_saved_content_status()
            if completion == "abandoned" and not silent:
                QMessageBox.information(
                    self, "抖音带货", "已关闭临时编辑页，未保存草稿或发布。"
                )
            if completion == "login_required":
                self._apply_login_required_state()
        if completion == "publish_completed":
            self._clear_current_batch_platform_choices()

    def _setup_generation_close_failed(
        self,
        close_token: int,
    ) -> None:
        if close_token != self._setup_close_token:
            return
        code = "cleanup_incomplete"
        self._setup_close_error_code = code
        self.platform_review_status.setText(
            f"平台设置采集器关闭未完成 · 错误码 {code}"
        )
        self._set_stage_error("content", code)
        self._sync_view()

    def _run_collector_action(
        self,
        collector_type: str,
        work,
        on_success,
        *,
        action_label: str,
    ) -> bool:
        generation_id = self._setup_generation_id
        if not generation_id or self.runner.is_running(self._COLLECTOR_TASK_KEY):
            return False
        self._collector_action_tokens[collector_type] += 1
        action_token = self._collector_action_tokens[collector_type]
        self._start_platform_collector_progress(
            generation_id,
            collector_type,
            action_token,
            action_label,
        )

        def controlled_work() -> object:
            try:
                return work()
            except douyin_commerce_collectors.DouyinCommerceCollectorError as error:
                return error.to_public_action_result()

        started = self.runner.run(
            self._COLLECTOR_TASK_KEY,
            with_progress=lambda _report: controlled_work(),
            on_success=lambda result: self._collector_action_succeeded(
                generation_id,
                collector_type,
                action_token,
                result,
                on_success,
            ),
            on_error=lambda message: self._collector_action_failed(
                generation_id, collector_type, action_token, message
            ),
            on_finished=lambda: self._collector_action_finished(
                generation_id,
                collector_type,
                action_token,
            ),
        )
        if not started:
            self._collector_action_failed(
                generation_id, collector_type, action_token, "collector_start_failed"
            )
            self._collector_action_finished(
                generation_id,
                collector_type,
                action_token,
            )
        return started

    def _collector_action_finished(
        self,
        generation_id: str,
        collector_type: str,
        action_token: int,
    ) -> None:
        self._finish_platform_collector_progress(
            generation_id,
            collector_type,
            action_token,
        )
        self._sync_view()

    def _collector_action_succeeded(
        self,
        generation_id: str,
        collector_type: str,
        action_token: int,
        result: object,
        on_success,
    ) -> None:
        if (
            generation_id != self._setup_generation_id
            or action_token != self._collector_action_tokens.get(collector_type)
        ):
            return
        if not isinstance(result, Mapping):
            return
        if (
            _normalized(result.get("setupGenerationId")) != generation_id
            or _normalized(result.get("collectorType")) != collector_type
        ):
            return
        result_instance = _normalized(result.get("collectorInstanceId"))
        if not result_instance:
            return
        if result.get("ok") is False:
            self._collector_action_failed(
                generation_id, collector_type, action_token, result
            )
            return
        if result.get("ok") is not True:
            return
        if "platformResultCount" in result:
            payload = {
                "platformResultCount": result.get("platformResultCount"),
                "candidates": result.get("candidates"),
            }
        else:
            payload = result.get("candidates", result.get("result", result))
        try:
            status = douyin_commerce_collectors.commerce_collector_manager.status(
                generation_id
            )
        except Exception:
            return
        if _normalized(status.get("setupGenerationId")) != generation_id:
            return
        if _normalized(status.get("generationState")) not in {
            "collecting",
            "ready",
        }:
            return
        current_instance = _normalized(
            self._collector_detail(status, collector_type).get("instanceId")
        )
        if not current_instance or result_instance != current_instance:
            return
        self._render_collector_status(status)
        on_success(payload)

    def _collector_action_failed(
        self,
        generation_id: str,
        collector_type: str,
        action_token: int,
        message: object,
    ) -> None:
        if (
            generation_id != self._setup_generation_id
            or action_token != self._collector_action_tokens.get(collector_type)
        ):
            return
        if not isinstance(message, Mapping):
            return
        if (
            _normalized(message.get("setupGenerationId")) != generation_id
            or _normalized(message.get("collectorType")) != collector_type
            or message.get("ok") is not False
        ):
            return
        result_instance = _normalized(message.get("collectorInstanceId"))
        if not result_instance:
            return
        try:
            status = douyin_commerce_collectors.commerce_collector_manager.status(
                generation_id
            )
        except Exception:
            return
        if _normalized(status.get("setupGenerationId")) != generation_id:
            return
        if _normalized(status.get("generationState")) not in {
            "collecting",
            "ready",
        }:
            return
        current_instance = _normalized(
            self._collector_detail(status, collector_type).get("instanceId")
        )
        if not current_instance or result_instance != current_instance:
            return
        code = self._public_collector_error_code(message.get("errorCode"))
        if code == "login_required":
            self._handle_login_required()
            return
        collectors = dict(status.get("collectors") or {})
        collectors[collector_type] = "failed"
        details = dict(status.get("collectorDetails") or {})
        detail = dict(details.get(collector_type) or {})
        detail.update({"state": "failed", "errorCode": code})
        details[collector_type] = detail
        status = {**status, "setupGenerationId": generation_id, "collectors": collectors, "collectorDetails": details}
        self._last_failed_collector_type = collector_type
        self._render_collector_status(status)
        _LOGGER.warning(
            "抖音设置采集失败 collector=%s errorCode=%s", collector_type, code
        )

    def _retry_last_failed_collector(self) -> None:
        collector_type = self._last_failed_collector_type
        generation_id = self._setup_generation_id
        if not collector_type or not generation_id:
            return

        def retry_succeeded(status: object) -> None:
            if not isinstance(status, Mapping):
                return
            if collector_type == "favorite_music":
                self._clear_music_candidates()
            elif collector_type in {"domestic_location", "local_location"}:
                target_scope = (
                    douyin_commerce_service.LOCATION_SCOPE_DOMESTIC
                    if collector_type == "domestic_location"
                    else douyin_commerce_service.LOCATION_SCOPE_LOCAL
                )
                current = self._batch_location_state()
                if _normalized(current.get("scope")) == target_scope:
                    current["candidates"] = []
                    self._batch_location_searches[_BATCH_SHARED_LOCATION_SEARCH_KEY] = current
                    self._render_batch_item_rows()
            self._render_collector_status(status)

        self._run_collector_action(
            collector_type,
            lambda: douyin_commerce_collectors.commerce_collector_manager.retry_collector(
                generation_id, collector_type
            ),
            retry_succeeded,
            action_label={
                "domestic_location": "正在重试国内地点",
                "favorite_music": "正在重试收藏音乐",
                "local_location": "正在重试本地地点",
            }[collector_type],
        )

    def continue_after_content(self) -> None:
        """为 1 至 20 条视频统一建立隔离的平台设置采集代际。"""

        if self.selected_video_count() >= 1:
            if not self._batch_content_is_valid():
                QMessageBox.warning(self, "继续", "请选择账号、1 至 20 条视频并填写作品文案。")
                return
            try:
                payload = self.collect_upload_payload()
            except (ValueError, douyin_commerce_service.DouyinCommerceError) as exc:
                QMessageBox.warning(self, "继续", str(exc))
                return
            self._start_setup_generation(payload)
            return

        try:
            payload = self.collect_upload_payload()
        except (ValueError, douyin_commerce_service.DouyinCommerceError) as exc:
            QMessageBox.warning(self, "继续", str(exc))
            return
        change_kind = self._content_change_kind()
        if change_kind == "none":
            self._go_to_step(1)
            return
        if change_kind == "sync":
            self._start_content_sync(payload)
            return
        self.start_upload(payload)

    def start_upload(self, payload: dict | None = None) -> None:
        """新建上传会话；仅账号或视频变化时由内容页分流至此。"""

        if payload is None:
            try:
                payload = self.collect_upload_payload()
            except (ValueError, douyin_commerce_service.DouyinCommerceError) as exc:
                QMessageBox.warning(self, "上传视频并继续", str(exc))
                return
        if self.runner.is_running("douyin_commerce_upload"):
            return
        self._pending_upload_payload = dict(payload)
        self.upload_button.setEnabled(False)
        self.upload_button.setText("正在上传")
        self.status_badge.setText("后台上传中")
        self.login_required_frame.setVisible(False)
        self._set_commerce_progress(
            {"phase": "checking_session", "state": "running"}
        )

        started = self.runner.run(
            "douyin_commerce_upload",
            with_progress=lambda report: douyin_commerce_session.commerce_session_manager.start_upload(
                payload, on_progress=report
            ),
            on_progress=self._set_commerce_progress,
            on_success=self._handle_upload_result,
            on_error=self._upload_failed,
            on_finished=self._sync_view,
        )
        if not started:
            self._pending_upload_payload = None
        self._sync_view()

    def _start_content_sync(self, payload: dict) -> None:
        """同步当前编辑页的文字字段；不创建浏览器、不重新上传视频。"""

        session_id = self._session_id
        if not session_id or self.runner.is_running("douyin_commerce_sync_content"):
            return
        self.upload_button.setEnabled(False)
        self.upload_button.setText("正在同步")
        self._set_commerce_progress({"phase": "syncing_content", "state": "running"})
        self.runner.run(
            "douyin_commerce_sync_content",
            with_progress=lambda report: douyin_commerce_session.commerce_session_manager.synchronize_content(
                session_id,
                payload,
                on_progress=report,
            ),
            on_progress=self._set_commerce_progress,
            on_success=lambda result: self._content_sync_succeeded(payload, result),
            on_error=self._content_sync_failed,
            on_finished=self._sync_view,
        )
        self._sync_view()

    def _content_sync_succeeded(self, payload: dict, result: object) -> None:
        """只在平台回读成功后更新本地编辑快照。"""

        if not isinstance(result, dict) or result.get("status") != "synced":
            self._content_sync_failed("抖音内容同步未能获得页面回读")
            return
        confirmed_tags = result.get("tags")
        if isinstance(confirmed_tags, list):
            self._set_tags(confirmed_tags)
        self._uploaded_editor_payload = {
            **dict(payload),
            "title": str(result.get("title") or payload.get("title") or ""),
            "description": str(
                result.get("description") or payload.get("description") or ""
            ),
            "tags": list(confirmed_tags) if isinstance(confirmed_tags, list) else list(payload.get("tags") or []),
        }
        self._preflight_fingerprint = ""
        self._clear_commerce_progress()
        self._clear_stage_error("content")
        self.content_notice.setVisible(False)
        self._go_to_step(1)

    def _content_sync_failed(self, message: str) -> None:
        """同步失败时保留本地表单；失效会话才退回重新上传。"""

        self._clear_commerce_progress()
        normalized = _normalized(message)
        if any(
            marker in normalized
            for marker in ("编辑页已关闭", "没有可继续", "会话已更新", "编辑会话不可写入")
        ):
            session_id = self._session_id
            self._session_id = ""
            self._uploaded_editor_payload = None
            if session_id:
                douyin_commerce_session.commerce_session_manager.close(session_id)
        self._platform_action_error("content", message)

    def _set_commerce_progress(self, event: object) -> None:
        """显示受控上传阶段，拒绝把执行器诊断、页面文字或凭据带入客户端。"""

        phase = _normalized((event or {}).get("phase")) if isinstance(event, dict) else ""
        progress = self._UPLOAD_PROGRESS.get(phase)
        if progress is None:
            return
        value, label = progress
        self._commerce_progress_phase = phase
        self.operation_progress.setValue(value)
        self.operation_progress_label.setText(label)
        self.operation_progress_frame.setVisible(True)
        if self.pages.currentIndex() == 1:
            self.platform_session_status.setText(label)
            self.platform_review_status.setText(label)

    def _clear_commerce_progress(self) -> None:
        self._commerce_progress_phase = ""
        self.operation_progress.setValue(0)
        self.operation_progress_frame.setVisible(False)

    def _start_platform_collector_progress(
        self,
        generation_id: str,
        collector_type: str,
        action_token: int,
        label: str,
    ) -> None:
        """显示当前单队列采集动作的不确定进度。"""

        self._platform_collector_progress_owner = (
            _normalized(generation_id),
            _normalized(collector_type),
            int(action_token),
        )
        self._platform_collector_progress_label_text = _normalized(label)
        self._platform_collector_progress_started = time.monotonic()
        self.platform_collector_progress.setRange(0, 0)
        self.platform_collector_progress_frame.setVisible(True)
        self._update_platform_collector_progress()
        self._platform_collector_progress_timer.start()

    def _update_platform_collector_progress(self) -> None:
        if self._platform_collector_progress_owner is None:
            return
        elapsed = max(
            0,
            int(time.monotonic() - self._platform_collector_progress_started),
        )
        self.platform_collector_progress_label.setText(
            f"{self._platform_collector_progress_label_text} · 已等待 {elapsed} 秒"
        )

    def _finish_platform_collector_progress(
        self,
        generation_id: str,
        collector_type: str,
        action_token: int,
    ) -> None:
        owner = (
            _normalized(generation_id),
            _normalized(collector_type),
            int(action_token),
        )
        if owner != self._platform_collector_progress_owner:
            return
        self._reset_platform_collector_progress()

    def _reset_platform_collector_progress(self) -> None:
        self._platform_collector_progress_timer.stop()
        self._platform_collector_progress_owner = None
        self._platform_collector_progress_label_text = ""
        self._platform_collector_progress_started = 0.0
        if hasattr(self, "platform_collector_progress_label"):
            self.platform_collector_progress_label.clear()
            self.platform_collector_progress_frame.setVisible(False)

    def _handle_upload_result(self, result: object) -> None:
        if isinstance(result, dict) and result.get("status") == "needs_login":
            self._handle_login_required()
            return
        if not isinstance(result, dict) or not _normalized(result.get("sessionId")):
            self._upload_failed("抖音上传会话未能建立")
            return
        self._upload_succeeded(result)

    def _upload_failed(self, message: str) -> None:
        self._clear_commerce_progress()
        # 新上传开始时会主动关闭旧编辑页；失败后不能把旧 sessionId 继续投影为可用。
        if self._pending_upload_payload is not None:
            self._session_id = ""
            self._uploaded_editor_payload = None
        self._pending_upload_payload = None
        self._platform_action_error("content", message)

    def _handle_login_required(self) -> None:
        """登录失效时只给出账号管理入口，绝不在当前发布页接管登录。"""

        self._reset_platform_collector_progress()
        if (
            self._setup_generation_id
            or self._setup_generation_cleanup_required
            or self.runner.is_running(self._SETUP_GENERATION_TASK_KEY)
        ):
            self._dispatch_setup_generation_close(
                reason="login_required",
                completion="login_required",
                silent=True,
            )
            return
        self._apply_login_required_state()

    def _apply_login_required_state(self) -> None:
        """采集代际已完全关闭后，再清理本批选择并给出账号入口。"""

        self._clear_commerce_progress()
        self._session_id = ""
        self._clear_music_candidates()
        self._selected_music = None
        self.music_card.setText("尚未选择收藏音乐")
        self._locations = []
        self._selected_location_data = None
        self._pending_location = None
        self._location_applied = False
        self.location_result_list.clear()
        self.location_candidate_card.setText("尚未选择发布定位")
        self.location_applied_card.setText("尚未选择发布定位")
        self._clear_declaration("登录后重新上传，再选择声明")
        self._preflight_fingerprint = ""
        self._uploaded_editor_payload = None
        self._pending_upload_payload = None
        self.login_required_frame.setVisible(True)
        self.pages.setCurrentIndex(0)
        self._sync_view()

    def _upload_succeeded(self, result: dict) -> None:
        self._clear_commerce_progress()
        self._session_id = _normalized(result.get("sessionId"))
        self._clear_stage_error("content")
        self._clear_music_candidates()
        self._selected_music = None
        self.music_card.setText("尚未选择收藏音乐")
        self.music_status.setText(str(result.get("message") or "视频上传完成"))
        self.location_result_list.clear()
        self._locations = []
        self._selected_location_data = None
        self._pending_location = None
        self.location_candidate_card.setText("尚未选择发布定位")
        self.location_applied_card.setText("尚未选择发布定位")
        self._location_applied = False
        self.location_scope_combo.blockSignals(True)
        self.location_scope_combo.setCurrentIndex(2)
        self.location_scope_combo.blockSignals(False)
        self._clear_declaration("正在写入默认声明")
        self._cache_load_pending = True
        self._preflight_fingerprint = ""
        self._uploaded_editor_payload = dict(self._pending_upload_payload or {}) or None
        self._pending_upload_payload = None
        self.content_notice.setText("视频已上传一次；后续设置不会重复上传。")
        self.content_notice.setVisible(False)
        self._go_to_step(1)
        self._start_declaration_write(self._DEFAULT_CONTENT_DECLARATION)

    def _load_cached_favorite_music_candidates(self) -> None:
        """视频上传后优先读取当前账号本地缓存，不打开抖音编辑页。"""

        if not self._session_id:
            return
        session_id = self._session_id
        self._start_immediate_write(
            "music_cache",
            lambda: douyin_commerce_session.commerce_session_manager.cached_favorite_music(
                session_id
            ),
            lambda rows: self._show_music_candidates(rows, source="cache"),
            self._music_cache_load_failed,
        )

    def _load_favorite_music_candidates(self) -> None:
        """只展开账号本机缓存；批量草稿阶段绝不偷偷创建平台会话。"""

        # 新批量工作台在上传前就需要选择共享音乐。此时只能读取当前账号已
        # 同步到本机的安全缓存；缓存缺失时不创建浏览器、不上传，也不默认选歌。
        if self.selected_video_count() >= 1:
            # 首次点击同步到本地缓存后应直接展开卡片内候选，不能要求用户再点一次。
            self._load_batch_cached_favorite_music(open_candidate_list=True)
            return

        if not self._session_id:
            QMessageBox.warning(self, "选择收藏音乐", "请先上传视频。")
            return
        if self._music_candidates:
            self.music_combo.showPopup()
            return
        self.music_status.setText("暂无本地收藏音乐，请点击刷新。")
        self.music_status.setVisible(True)

    def _refresh_favorite_music_candidates(self) -> None:
        """用户明确刷新时才打开当前抖音编辑页的收藏列表。"""

        if self._setup_generation_id:
            generation_id = self._setup_generation_id
            self.music_status.setText("正在刷新收藏音乐…")
            self.music_status.setVisible(True)
            self._run_collector_action(
                "favorite_music",
                lambda: douyin_commerce_collectors.commerce_collector_manager.refresh_favorite_music(
                    generation_id
                ),
                lambda rows: self._show_music_candidates(
                    rows if isinstance(rows, list) else [], source="collector"
                ),
                action_label="正在刷新收藏音乐",
            )
            return
        if self.selected_video_count() >= 1 and not self._session_id:
            # 没有编辑会话时不能伪造“刷新成功”。引导用户保持在安全的本地
            # 选择阶段，实际平台读取会在后续明确的预检会话内进行，且不提交。
            self._load_batch_cached_favorite_music(refresh_requested=True)
            return

        if not self._session_id:
            QMessageBox.warning(self, "刷新收藏音乐", "请先上传视频。")
            return
        session_id = self._session_id
        self.music_status.setText("正在刷新收藏音乐…")
        self.music_status.setVisible(True)
        self._open_music_picker_after_load = self._start_immediate_write(
            "music_refresh",
            lambda: douyin_commerce_session.commerce_session_manager.refresh_favorite_music(
                session_id
            ),
            # 刷新完成后平台音乐抽屉已关闭，候选只能作为本地缓存展示；用户
            # 真正选择时会重新打开当前抽屉精确匹配，不能复用失效的 DOM marker。
            lambda rows: self._show_music_candidates(rows, source="cache"),
            self._music_load_failed,
        )

    def _load_batch_cached_favorite_music(
        self,
        *,
        refresh_requested: bool = False,
        open_candidate_list: bool = False,
    ) -> None:
        """读取当前账号的本地收藏音乐缓存，不触碰抖音页面。

        刷新按钮在未上传阶段只会重新读取本机缓存，避免把“刷新”误做成隐式
        上传/打开浏览器。若缓存为空，用户必须先通过受控预检读取真实收藏列表
        后再选择，页面不会提交任务。
        """

        account = self._selected_account() or {}
        try:
            account_id = int(account.get("id") or 0)
        except (TypeError, ValueError):
            account_id = 0
        if account_id <= 0:
            self.music_status.setText("请先选择抖音账号")
            self.music_status.setVisible(True)
            return
        try:
            rows = douyin_favorite_music_cache.list_cached_favorite_music(account_id)
        except Exception:
            rows = []
        if rows:
            self._show_music_candidates(rows, source="account-cache")
            self.music_status.setText("从本机收藏音乐缓存选择")
            if open_candidate_list:
                # _show_music_candidates 会先重建列表并同步控件状态；延迟到本轮
                # Qt 事件结束后再展开，避免首次点击被组合框原始鼠标事件吞掉。
                QTimer.singleShot(0, self._open_music_picker_if_ready)
        else:
            self._music_candidates = []
            self.music_candidate_list.clear()
            self.music_combo.blockSignals(True)
            try:
                self.music_combo.clear()
                self.music_combo.addItem("暂无本地收藏音乐", None)
            finally:
                self.music_combo.blockSignals(False)
            self.music_status.setText(
                "当前账号暂无本地收藏音乐缓存；请先受控读取收藏列表，选择后再继续。"
                if refresh_requested
                else "当前账号暂无本地收藏音乐缓存。"
            )
        self.music_status.setVisible(True)
        self._sync_view()

    def _show_music_candidates(
        self,
        rows: list[dict[str, str]],
        *,
        source: str = "session",
    ) -> None:
        self._music_candidates = [dict(item) for item in rows]
        self._music_candidate_source = source if self._music_candidates else ""
        if self.selected_video_count() >= 1 and source in {"session", "collector"}:
            self._cache_batch_music_candidates(self._music_candidates)
        self._clear_stage_error("music")
        self.music_candidate_list.clear()
        self.music_combo.blockSignals(True)
        try:
            self.music_combo.clear()
            self.music_combo.addItem("选择收藏音乐", None)
            for row in self._music_candidates:
                title = _normalized(row.get("title")) or "未命名音乐"
                creator = _normalized(row.get("creator")) or "未知作者"
                duration = _normalized(row.get("duration"))
                suffix = f" · {duration}" if duration else ""
                self.music_combo.addItem(f"{title} · {creator}{suffix}", dict(row))
                item = QListWidgetItem(f"{title} · {creator}{suffix}")
                item.setData(Qt.ItemDataRole.UserRole, dict(row))
                item.setToolTip(f"{title} · {creator}{suffix}")
                self.music_candidate_list.addItem(item)
            self._restore_music_combo(self._selected_music)
        finally:
            self.music_combo.blockSignals(False)
        self.music_status.setText(
            f"已加载 {len(self._music_candidates)} 首收藏音乐。"
            if self._music_candidates
            else "暂无可用收藏音乐，请点击刷新。"
        )
        self._sync_view()

    def _cache_batch_music_candidates(self, rows: list[dict[str, str]]) -> None:
        """把用户已主动读取到的收藏音乐安全落盘，供下次批量直接选择。"""

        account = self._selected_account() or {}
        try:
            account_id = int(account.get("id") or 0)
        except (TypeError, ValueError):
            account_id = 0
        if account_id <= 0:
            return
        try:
            douyin_favorite_music_cache.replace_cached_favorite_music(account_id, rows)
        except Exception as exc:
            _LOGGER.warning("保存抖音收藏音乐本地缓存失败：%s", _normalized(exc))

    def _music_cache_load_failed(self, message: str) -> None:
        self.music_status.setText("本地收藏音乐未读取完成，可点击刷新。")
        _LOGGER.warning("读取抖音收藏音乐本地缓存失败：%s", _normalized(message))
        self._sync_view()

    def _music_load_failed(self, message: str) -> None:
        self._open_music_picker_after_load = False
        self.music_status.setText("收藏音乐未读取完成，请重新打开选择。")
        self._platform_action_error("music", message)

    def _start_music_write(self, candidate: dict[str, str]) -> None:
        candidate = dict(candidate)
        music_id = _normalized(candidate.get("musicId"))
        if self._selected_music and music_id == _normalized(self._selected_music.get("musicId")):
            self._restore_music_combo(self._selected_music)
            return
        if not candidate or not self._session_id:
            QMessageBox.warning(self, "选择收藏音乐", "请先从当前收藏列表选择一首音乐。")
            return
        self._pending_music = candidate
        self.music_status.setText("正在写入音乐…")
        session_id = self._session_id
        self._start_immediate_write(
            "music",
            lambda: (
                douyin_commerce_session.commerce_session_manager.select_cached_favorite_music(
                    session_id, music_id
                )
                if self._music_candidate_source == "cache"
                else douyin_commerce_session.commerce_session_manager.select_favorite_music(
                    session_id, music_id
                )
            ),
            self._music_selected,
            self._immediate_music_failed,
        )

    def _music_selected(self, music: dict[str, str]) -> None:
        # 当前音乐抽屉的 DOM 标记会在选择完成后失效，但歌曲的公开身份仍可
        # 继续作为“更换音乐”的列表展示。下次点击会重新打开当前官方抽屉并按
        # musicId 精确核验，因此无需让用户手动刷新一次才能更换。
        reusable_candidates = [
            {
                key: _normalized(candidate.get(key))
                for key in ("musicId", "title", "creator", "duration")
            }
            for candidate in self._music_candidates
            if isinstance(candidate, dict) and _normalized(candidate.get("musicId"))
        ]
        self._selected_music = dict(music)
        if reusable_candidates:
            self._show_music_candidates(reusable_candidates, source="cache")
            self.music_candidate_list.setVisible(False)
            self.music_status.setText("当前音乐已确认；如需更换可直接选择，系统会重新核验平台收藏列表。")
        else:
            self._discard_music_candidates_for_reload()
        self._clear_stage_error("music")
        self.music_card.setText(self._music_display(self._selected_music))
        self._preflight_fingerprint = ""
        self._sync_view()

    def _immediate_music_failed(self, message: str) -> None:
        # 失败也不能再次使用旧弹窗的 DOM 标识，必须回到平台重新读取。
        self._discard_music_candidates_for_reload()
        self.music_status.setText("音乐未写入平台，请重新读取后选择。")
        self._platform_action_error("music", message)

    def search_locations(self) -> None:
        keyword = self.location_keyword.text()
        scope = self._selected_location_scope()
        if not self._setup_generation_id and not self._session_id:
            QMessageBox.warning(self, "搜索发布定位", "请先完成视频上传。")
            return
        if not scope:
            QMessageBox.warning(self, "搜索发布定位", "请先选择地点范围：本地或国内。")
            return
        self.location_status.setText(
            f"正在按“{douyin_commerce_service.location_scope_label(scope)}”范围，从当前抖音编辑页读取候选…"
        )
        if self._setup_generation_id:
            generation_id = self._setup_generation_id
            collector_type = (
                "domestic_location"
                if scope == douyin_commerce_service.LOCATION_SCOPE_DOMESTIC
                else "local_location"
            )
            self._run_collector_action(
                collector_type,
                lambda: douyin_commerce_collectors.commerce_collector_manager.search_locations(
                    generation_id, keyword, scope
                ),
                lambda rows: self._show_locations(
                    rows if isinstance(rows, list) else []
                ),
                action_label=(
                    "正在搜索国内地点"
                    if collector_type == "domestic_location"
                    else "正在搜索本地地点"
                ),
            )
            return
        session_id = self._session_id
        self._start_immediate_write(
            "location_search",
            lambda: douyin_commerce_session.commerce_session_manager.search_locations(
                session_id, keyword, scope
            ),
            self._show_locations,
            self._location_search_error,
        )

    def _show_locations(self, rows: list[dict[str, str]]) -> None:
        self._locations = [dict(row) for row in rows]
        self._selected_location_data = None
        self._pending_location = None
        self._clear_stage_error("location")
        self.location_result_list.blockSignals(True)
        self.location_result_list.clear()
        for row in self._locations:
            address = _normalized(row.get("address")) or "平台未返回完整地址"
            distance = _normalized(row.get("distance"))
            item = QListWidgetItem(
                f"{row.get('name') or ''}\n{address}{f' · {distance}' if distance else ''}"
            )
            # 结果卡固定留出两到三行，避免完整地址被 QListWidget 的默认单行
            # 委托省略。更长文字会换行，用户可在同一个列表直接核对。
            item.setSizeHint(QSize(0, 80))
            item.setData(Qt.ItemDataRole.UserRole, row)
            tooltip = self._location_display(row)
            item.setToolTip(tooltip)
            self.location_result_list.addItem(item)
        self.location_result_list.blockSignals(False)
        if not self._locations:
            self.location_status.setText("当前抖音编辑页未返回含完整地址的发布定位候选，请更换关键词")
        else:
            selected_scope = self._selected_location_scope()
            scope_label = (
                douyin_commerce_service.location_scope_label(selected_scope)
                if selected_scope
                else "未确认"
            )
            self.location_status.setText(
                f"已从当前抖音编辑页的“{scope_label}”范围读取 {len(self._locations)} 个发布定位候选，请选择一项。"
            )
        self._location_applied = False
        self._sync_view()

    def _location_search_error(self, message: str) -> None:
        self.location_status.setText("未能从当前编辑页读取完整地点，请按下方提示重新搜索。")
        self._platform_action_error("location", message)

    def _start_location_write(self, location: dict[str, str]) -> None:
        location = dict(location)
        if not location or not self._session_id:
            QMessageBox.warning(self, "选择发布定位", "请选择一项平台发布定位候选。")
            return
        self._pending_location = location
        self.location_status.setText("正在选择发布定位、确认带货模式并回读…（本次不绑定门店）")
        self.location_candidate_card.setText("正在由当前抖音编辑页确认发布定位…")
        session_id = self._session_id
        self._start_immediate_write(
            "location",
            lambda: douyin_commerce_session.commerce_session_manager.apply_location(
                session_id, location
            ),
            self._location_applied_success,
            self._immediate_location_failed,
        )

    def _location_applied_success(self, result: dict) -> None:
        location = result.get("location") if isinstance(result, dict) else None
        if not isinstance(location, dict):
            self._location_search_error("抖音未返回完整发布定位回读，已停止后续设置")
            return
        self._selected_location_data = dict(location)
        self._pending_location = None
        self._location_applied = True
        self._clear_stage_error("location")
        self.location_applied_card.setText(
            self._location_display(dict(location))
        )
        self.location_status.setText("已选择发布定位")
        self._preflight_fingerprint = ""
        self._sync_view()

    def _immediate_location_failed(self, message: str) -> None:
        self._pending_location = None
        self._selected_location_data = None
        self._location_applied = False
        self.location_result_list.blockSignals(True)
        self.location_result_list.clearSelection()
        self.location_result_list.setCurrentRow(-1)
        self.location_result_list.clear()
        self.location_result_list.blockSignals(False)
        self._locations = []
        self.location_candidate_card.setText("请重新搜索并选择平台发布定位候选")
        self.location_status.setText("地点未写入平台，请重新搜索后选择。")
        self.location_view_stack.setCurrentWidget(self.location_search_view)
        self._platform_action_error("location", message)

    def _start_declaration_write(self, declaration: str) -> None:
        declaration = _normalized(declaration)
        if not declaration or not self._session_id:
            if not declaration:
                QMessageBox.warning(self, "作品内容声明", "请先选择一项作品内容声明。")
            return
        self._pending_declaration = declaration
        self.declaration_status.setText("正在将用户所选声明写入抖音编辑页并回读…")
        session_id = self._session_id
        self._start_immediate_write(
            "declaration",
            lambda: douyin_commerce_session.commerce_session_manager.select_content_declaration(
                session_id, declaration
            ),
            self._declaration_applied_success,
            self._immediate_declaration_failed,
        )

    def _declaration_applied_success(self, declaration: str) -> None:
        selected = _normalized(declaration)
        if selected != self._pending_declaration:
            self._immediate_declaration_failed(
                "抖音页面回读与用户所选声明不一致，已停止后续设置"
            )
            return
        self._set_selected_declaration(selected)
        self._declaration_applied = True
        self._confirmed_declaration = selected
        self._pending_declaration = ""
        self._clear_stage_error("declaration")
        self.declaration_card.setText(selected)
        self.declaration_status.setText("")
        self._preflight_fingerprint = ""
        self._sync_view()

    def _immediate_declaration_failed(self, message: str) -> None:
        self._pending_declaration = ""
        self._declaration_applied = bool(self._confirmed_declaration)
        self._set_selected_declaration(
            self._confirmed_declaration or self._DEFAULT_CONTENT_DECLARATION
        )
        self.declaration_status.setText("声明未完成平台回读，请按下方提示重新选择。")
        self._platform_action_error("declaration", message)

    def start_preflight(self) -> None:
        if self.selected_video_count() >= 1:
            self.start_batch_preflight()
            return
        try:
            payload = self.collect_payload("preflight")
        except Exception as exc:
            QMessageBox.warning(self, "开始预检", str(exc))
            self._sync_view()
            return
        if not self._session_id:
            QMessageBox.warning(self, "开始预检", "上传会话已结束，请重新上传视频。")
            return
        task = task_service.create_pending_task([payload], mode="oneclick_preflight")
        self._active_task_id = int(task["id"])
        task_service.mark_task_running(self._active_task_id, "抖音带货开始在同一编辑会话执行预检")
        self.preflight_button.setEnabled(False)
        self.validation_label.setText(f"预检任务 {task.get('taskNo')} 正在回读平台字段…")
        self.runner.run(
            "douyin_commerce_preflight",
            lambda: douyin_commerce_session.commerce_session_manager.preflight(self._session_id, payload),
            on_success=lambda result: self._preflight_succeeded(task, payload, result),
            on_error=lambda message: self._preflight_failed(task, message),
            on_finished=self._preflight_finished,
        )
        self._sync_view()

    def _preflight_succeeded(self, task: dict, payload: dict, result: dict) -> None:
        task_service.record_task_event(
            int(task["id"]),
            "douyin_commerce_staged_preflight_readback",
            str(result.get("message") or "抖音带货分步预检已完成"),
        )
        task_service.mark_platform_result(
            int(task["id"]),
            3,
            ok=True,
            message=str(result.get("message") or "抖音带货预检完成"),
            content_type="video",
        )
        self._preflight_fingerprint = self._payload_fingerprint("preflight")
        mode_label = "定时发布" if payload.get("enableTimer") is True else "立即发表"
        self.validation_label.setText(f"预检已完成并回读字段；尚未提交{mode_label}。")
        QMessageBox.information(
            self,
            "预检完成",
            f"任务 {task.get('taskNo')} 已完成预检。\n尚未保存草稿或提交{mode_label}，请核对后再确认。",
        )

    def _preflight_failed(self, task: dict, message: str) -> None:
        task_service.mark_platform_result(
            int(task["id"]),
            3,
            ok=False,
            message=f"预检任务异常：{message}",
            content_type="video",
        )
        self._preflight_fingerprint = ""
        self.validation_label.setText(message)
        QMessageBox.warning(self, "预检未通过", message)

    def _preflight_finished(self) -> None:
        self._active_task_id = None
        self._sync_view()

    def open_submit_confirmation(self) -> None:
        if self.selected_video_count() >= 1:
            self.open_batch_submit_confirmation()
            return
        action_label = "确认定时提交" if self.timer_enabled.isChecked() else "确认立即发表"
        try:
            payload = self.collect_payload("publish")
        except Exception as exc:
            QMessageBox.warning(self, action_label, str(exc))
            self._sync_view()
            return
        if not self._can_review():
            QMessageBox.warning(
                self,
                action_label,
                "请先完成视频上传、音乐、地点、作品内容声明及定时设置。",
            )
            self._sync_view()
            return
        task = task_service.create_pending_task([payload], mode="oneclick_publish")
        self._active_task_id = int(task["id"])
        mode_label = "定时提交" if payload.get("enableTimer") is True else "立即发表"
        task_service.mark_task_running(self._active_task_id, f"抖音带货开始最终{mode_label}")
        self._preflight_fingerprint = ""
        self.validation_label.setText(f"{mode_label}任务 {task.get('taskNo')} 正在等待平台回执…")
        self.runner.run(
            "douyin_commerce_submit",
            lambda: douyin_commerce_session.commerce_session_manager.submit(
                self._session_id, payload, self._active_task_id
            ),
            on_success=lambda result: self._submit_succeeded(task, result),
            on_error=lambda message: self._submit_failed(task, message),
            on_finished=self._submit_finished,
        )
        self._start_douyin_verification_polling()
        self._sync_view()

    def _start_douyin_verification_polling(self) -> None:
        """只在最终提交运行时轮询 Broker 的本机内存请求。"""

        if self._verification_task_id() is None:
            return
        if not self._douyin_verification_poll_timer.isActive():
            self._douyin_verification_poll_timer.start()
        self._poll_douyin_verification()

    def _poll_douyin_verification(self) -> None:
        """按任务号打开唯一原生验证对话框，不执行浏览器操作。"""

        task_id = self._verification_task_id()
        if task_id is None:
            return
        request_id = verification_broker.request_for_task(task_id)
        if not request_id or request_id == self._douyin_verification_request_id:
            return
        if self._douyin_verification_dialog is not None:
            self._douyin_verification_dialog.accept()
        dialog_kwargs = {"broker": verification_broker, "parent": self}
        if self._verification_item_context:
            dialog_kwargs.update(
                item_index=self._verification_item_context.get("index"),
                item_total=self._verification_item_context.get("total"),
                item_label=str(self._verification_item_context.get("label") or ""),
            )
        dialog = DouyinVerificationDialog(request_id, **dialog_kwargs)
        self._douyin_verification_dialog = dialog
        self._douyin_verification_request_id = request_id
        dialog.show()

    def _verification_task_id(self) -> int | None:
        """返回仍在运行的验证任务号，批量任务优先于旧单条任务。"""

        for value in (self._batch_task_id, self._active_task_id):
            try:
                task_id = int(value or 0)
            except (TypeError, ValueError):
                task_id = 0
            if task_id > 0:
                return task_id
        return None

    def _cleanup_douyin_verification(self) -> None:
        """任务收束时停止轮询、关闭对话框并清空对应内存请求。"""

        self._douyin_verification_poll_timer.stop()
        request_id = self._douyin_verification_request_id
        if not request_id:
            task_id = self._verification_task_id()
            if task_id is not None:
                request_id = verification_broker.request_for_task(task_id) or ""
        if self._douyin_verification_dialog is not None:
            self._douyin_verification_dialog.accept()
        if request_id:
            verification_broker.clear(request_id)
        self._douyin_verification_dialog = None
        self._douyin_verification_request_id = ""
        self._verification_item_context = {}

    def _submit_succeeded(self, task: dict, result: dict) -> None:
        self._cleanup_douyin_verification()
        scheduled = result.get("scheduled") is True
        mode_label = "定时提交" if scheduled else "立即发表"
        task_service.record_task_event(
            int(task["id"]),
            "douyin_commerce_scheduled_readback" if scheduled else "douyin_commerce_publish_readback",
            str(result.get("message") or f"抖音带货{mode_label}已回读"),
        )
        task_service.mark_platform_result(
            int(task["id"]),
            3,
            ok=True,
            message=str(result.get("message") or f"抖音带货{mode_label}完成"),
            content_type="video",
            event_type="platform_publish",
        )
        self._session_id = ""
        self._active_task_id = None
        QMessageBox.information(self, f"{mode_label}完成", str(result.get("message") or "平台已回读发布结果"))

    def _submit_failed(self, task: dict, message: str) -> None:
        self._cleanup_douyin_verification()
        task_service.mark_platform_result(
            int(task["id"]),
            3,
            ok=False,
            message=f"最终提交异常：{message}",
            content_type="video",
            event_type="platform_publish",
        )
        # 最终提交流程结束时会关闭临时编辑页，避免把未知页面状态误复用到
        # 下一次提交。失败不代表已定时；如需重试，必须重新上传并预检。
        self._session_id = ""
        self._declaration_applied = False
        self._preflight_fingerprint = ""
        self.validation_label.setText(message)
        QMessageBox.warning(self, "最终提交未完成", message)

    def _submit_finished(self) -> None:
        self._cleanup_douyin_verification()
        self._active_task_id = None
        self._sync_view()

    def _reset_button(self, control, text: str) -> None:
        """后台任务清理后恢复按钮，并以最终空闲状态刷新整页。

        ``BackgroundTaskRunner`` 会先移除运行中的任务，再调用 ``on_finished``。
        成功回调发生得更早，彼时页面仍会把音乐任务视为“正在处理”，从而禁用
        地点控件。这里必须在任务真正清理后统一刷新，不能仅把当前按钮设为可用。
        """

        control.setText(text)
        self._sync_view()

    def _show_error(self, title: str, message: str) -> None:
        QMessageBox.warning(self, title, message)
        self._sync_view()

    def abandon_session(self) -> None:
        if not self._session_id and not self._setup_generation_id:
            return
        answer = QMessageBox.question(
            self,
            "放弃本次上传",
            "将关闭本次抖音临时编辑页，不保存草稿、不提交发布。是否继续？",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._abandon_session(silent=True)
        self.pages.setCurrentIndex(0)
        self._sync_view()

    def return_from_review(self) -> None:
        """从检查页安全返回；会话结束后改为开启下一条内容准备。"""

        if self._session_id:
            self._go_to_step(1)
            return
        self.start_new_content()

    def start_new_content(self) -> None:
        """结束临时会话并回到内容准备，不删除本机已保存内容。"""

        if self._busy():
            return
        self._abandon_session(silent=True)
        self.pages.setCurrentIndex(0)
        self._sync_view()

    def _abandon_session(self, *, silent: bool) -> None:
        session_id = self._session_id
        setup_active = bool(
            self._setup_generation_id or self._setup_generation_cleanup_required
        ) or self.runner.is_running(self._SETUP_GENERATION_TASK_KEY)
        self._session_id = ""
        if session_id:
            douyin_commerce_session.commerce_session_manager.close(session_id)
        if setup_active:
            self._dispatch_setup_generation_close(
                reason="user_abandon",
                completion="abandoned",
                silent=silent,
            )
            return
        self._reset_platform_settings_after_abandon()
        self._uploaded_editor_payload = None
        self._pending_upload_payload = None
        # 放弃只影响临时会话；从本机重新读取保存状态，确保恢复入口立即回到正确状态。
        self._refresh_saved_content_status()
        self._sync_view()
        if not silent:
            QMessageBox.information(self, "抖音带货", "已关闭临时编辑页，未保存草稿或发布。")

    def shutdown(self) -> bool:
        """客户端退出收口；只有启动任务已停且采集器严格归零才返回成功。"""

        self._reset_platform_collector_progress()
        self._shutdown_requested.set()
        self._batch_executor.request_shutdown(source="client_shutdown")
        batch_finished = True
        if self.runner.is_running(_BATCH_RUN_KEY):
            cancel_pending = getattr(self.runner, "cancel_pending", None)
            cancelled = bool(
                callable(cancel_pending) and cancel_pending(_BATCH_RUN_KEY)
            )
            if not cancelled:
                wait_for_finished = getattr(self.runner, "wait_for_finished", None)
                batch_finished = bool(
                    callable(wait_for_finished)
                    and wait_for_finished(
                        _BATCH_RUN_KEY,
                        self._SHUTDOWN_WAIT_SECONDS,
                    )
                )
        if not batch_finished:
            return False

        setup_finished = True
        if self.runner.is_running(self._SETUP_GENERATION_TASK_KEY):
            cancel_pending = getattr(self.runner, "cancel_pending", None)
            cancelled = bool(
                callable(cancel_pending)
                and cancel_pending(self._SETUP_GENERATION_TASK_KEY)
            )
            if not cancelled:
                wait_for_finished = getattr(self.runner, "wait_for_finished", None)
                setup_finished = bool(
                    callable(wait_for_finished)
                    and wait_for_finished(
                        self._SETUP_GENERATION_TASK_KEY,
                        self._SHUTDOWN_WAIT_SECONDS,
                    )
                )

        collectors_closed = False
        if setup_finished:
            result = self._close_setup_generation("client_shutdown")
            if not self._collector_close_is_complete(result):
                result = self._close_setup_generation("client_shutdown")
            collectors_closed = self._collector_close_is_complete(result)

        legacy_session_closed = True
        session_id = self._session_id
        self._session_id = ""
        if session_id:
            try:
                douyin_commerce_session.commerce_session_manager.close(session_id)
            except Exception:
                legacy_session_closed = False
        return setup_finished and collectors_closed and legacy_session_closed

    def _reset_platform_settings_after_abandon(self) -> None:
        """将平台设置恢复为一次全新上传的默认状态。

        内容准备中的账号、视频、标题、文案和话题保留；音乐、地点、
        声明与定时属于已放弃会话的发布意图，必须全部丢弃。
        """

        self._reset_platform_collector_progress()
        self._clear_music_candidates()
        self._selected_music = None
        self._collector_status = {}
        self._last_failed_collector_type = ""
        self._setup_start_error_code = ""
        self._setup_close_error_code = ""
        self._staged_music_confirmed = False
        self._staged_location_confirmed = False
        self._staged_declaration_confirmed = False
        self.retry_collector_button.setVisible(False)
        self.domestic_collector_status.setText("国内地点：等待进入设置")
        self.music_collector_status.setText("收藏音乐：点击刷新后启动")
        self.local_collector_status.setText("本地点：首次搜索时启动")
        self.music_card.setText("尚未选择收藏音乐")
        self.music_status.setText("本次上传会话已结束")
        self._batch_locations = {}
        self._batch_location_searches = {}
        self._batch_schedule_overrides = {}
        self._batch_location_feedback = ""
        self._batch_item_rows_signature = None
        self._locations = []
        self._selected_location_data = None
        self._pending_location = None
        self.location_result_list.clear()
        self.location_keyword.blockSignals(True)
        self.location_keyword.clear()
        self.location_keyword.blockSignals(False)
        self.location_candidate_card.setText("尚未选择发布定位")
        self.location_applied_card.setText("尚未选择发布定位")
        self._location_applied = False
        self.location_scope_combo.blockSignals(True)
        self.location_scope_combo.setCurrentIndex(
            self.location_scope_combo.findData(
                douyin_commerce_service.LOCATION_SCOPE_DOMESTIC
            )
        )
        self.location_scope_combo.blockSignals(False)
        self.batch_location_commission_combo.blockSignals(True)
        self.batch_location_commission_combo.setCurrentIndex(
            self.batch_location_commission_combo.findData(
                DEFAULT_COMMISSION_FILTER
            )
        )
        self.batch_location_commission_combo.blockSignals(False)
        self.batch_location_scope_combo.blockSignals(True)
        self.batch_location_scope_combo.setCurrentIndex(
            self.batch_location_scope_combo.findData(
                douyin_commerce_service.LOCATION_SCOPE_DOMESTIC
            )
        )
        self.batch_location_scope_combo.blockSignals(False)
        self.batch_location_keyword.blockSignals(True)
        self.batch_location_keyword.clear()
        self.batch_location_keyword.blockSignals(False)
        self._clear_declaration("请重新上传视频后选择地点与作品内容声明")
        self.batch_publish_mode.blockSignals(True)
        self.batch_publish_mode.setCurrentIndex(
            self.batch_publish_mode.findData("immediate")
        )
        self.batch_publish_mode.blockSignals(False)
        self.batch_timer_enabled.blockSignals(True)
        self.batch_timer_enabled.setChecked(False)
        self.batch_timer_enabled.blockSignals(False)
        self.timer_enabled.blockSignals(True)
        self.timer_enabled.setChecked(False)
        self.timer_enabled.blockSignals(False)
        self.batch_interval_minutes.blockSignals(True)
        self.batch_interval_minutes.setValue(30)
        self.batch_interval_minutes.blockSignals(False)
        default_schedule = self._default_schedule_datetime()
        self.schedule_date.blockSignals(True)
        self.schedule_date.setDate(
            QDate(default_schedule.year, default_schedule.month, default_schedule.day)
        )
        self.schedule_date.blockSignals(False)
        self.schedule_time.blockSignals(True)
        self.schedule_time.setTime(QTime(16, 0))
        self.schedule_time.blockSignals(False)
        self._schedule_date_auto_default = True
        self._preflight_fingerprint = ""
        self._batch_preflight_fingerprint = ""
        self._batch_progress_text = ""
        self._batch_result_feedback = ""
        self._batch_pause_requested = False
        self._batch_editor_session_ended = False
        for stage in ("music", "location", "declaration", "schedule"):
            self._clear_stage_error(stage)

    def _clear_current_batch_platform_choices(self) -> None:
        """明确完成后清除本批发布意图，账号候选缓存仍保留在独立缓存层。"""

        self._reset_platform_collector_progress()
        self._clear_music_candidates()
        self._selected_music = None
        self._pending_music = None
        self._batch_locations = {}
        self._batch_location_searches = {}
        self._batch_schedule_overrides = {}
        self.batch_location_commission_combo.blockSignals(True)
        self.batch_location_commission_combo.setCurrentIndex(
            self.batch_location_commission_combo.findData(
                DEFAULT_COMMISSION_FILTER
            )
        )
        self.batch_location_commission_combo.blockSignals(False)
        self._locations = []
        self._selected_location_data = None
        self._pending_location = None
        self.location_result_list.clear()
        self._location_applied = False
        self._set_selected_declaration(self._DEFAULT_CONTENT_DECLARATION)
        self._confirmed_declaration = ""
        self._pending_declaration = ""
        self._declaration_applied = False
        self._staged_music_confirmed = False
        self._staged_location_confirmed = False
        self._staged_declaration_confirmed = False
        self._batch_preflight_fingerprint = ""
        self._preflight_fingerprint = ""

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 固定事件名
        if self._session_id and not self._busy():
            self._abandon_session(silent=True)
        super().closeEvent(event)
