# -*- coding: utf-8 -*-
"""抖音带货的分步桌面工作流。

抖音的音乐、位置和作品内容声明只能在视频上传后的编辑页完成。本页因此不是
把所有字段堆在一个表单，而是明确分为：内容准备 → 同一编辑会话中的音乐、定位、
作品声明与定时 → 预检与提交。一次上传对应
一个仅内存中的受控编辑会话；不保存草稿，也不会在没有最终确认时点击发表。
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from PyQt6.QtCore import QDate, QPoint, QSize, QTime, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import (
    QButtonGroup,
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
    QStackedWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from app_core import (
    account_service,
    douyin_commerce_draft_service,
    douyin_commerce_service,
    douyin_commerce_session,
    media_service,
    task_service,
)
from app_core.douyin_verification import verification_broker
from app_core.paths import AVATAR_DIR

from .background_task import BackgroundTaskRunner
from .common import button
from .douyin_verification_dialog import DouyinVerificationDialog
from .runtime_log import ExecutionLogPanel


_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_LOGGER = logging.getLogger(__name__)
_ACCOUNT_AVATAR_SIZE = 54
_VIDEO_PICKER_NAME_LIMIT = 32
_VIDEO_PICKER_MAX_WIDTH = 440
_VIDEO_PICKER_MAX_HEIGHT = 480
_VIDEO_PICKER_ROW_HEIGHT = 68


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
    """首次点击读取收藏音乐；已读取时展开卡片内候选列表。"""

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


class DouyinCommercePage(QWidget):
    """单账号、单视频的抖音带货分步向导。"""

    request_account_management = pyqtSignal()

    # 用户看到的是两个配置阶段：先在本机准备内容，视频上传后再进入同一编辑会话
    # 完成平台设置。音乐、定位、声明可任意先后选择；每次平台写入仍会串行回读，
    # 只是不用四张割裂的表单页强制用户按界面顺序完成。
    _STEPS = ("内容准备", "平台设置", "检查与提交")
    _STEP_HINTS = (
        "选择账号、视频并完成本地内容准备。",
        "在同一抖音编辑页选择音乐、定位、作品声明与发布方式。",
        "只读预检通过后，再进行一次独立提交确认。",
    )
    _PLATFORM_STAGE_ORDER = ("blocked", "music", "location", "declaration", "schedule")
    _IMMEDIATE_WRITE_KEY = "douyin_commerce_immediate_write"
    _DEFAULT_CONTENT_DECLARATION = "无需添加自主声明"
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
        self._music_candidates: list[dict[str, str]] = []
        self._selected_music: dict[str, str] | None = None
        self._pending_music: dict[str, str] | None = None
        self._open_music_picker_after_load = False
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
        self._douyin_verification_poll_timer = QTimer(self)
        self._douyin_verification_poll_timer.setInterval(250)
        self._douyin_verification_poll_timer.timeout.connect(
            self._poll_douyin_verification
        )
        # 上传快照只用于区分“内容同步”和“重新上传”，不保存平台会话或凭据。
        self._uploaded_editor_payload: dict | None = None
        self._pending_upload_payload: dict | None = None
        self._saved_content_available = False
        self._tag_values: list[str] = []
        self._tag_history: list[str] = []
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
        dock.setMinimumHeight(74)
        layout = QHBoxLayout(dock)
        layout.setContentsMargins(22, 15, 20, 15)
        layout.setSpacing(14)
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
        self.operation_dock_upload.clicked.connect(self.continue_after_content)
        layout.addWidget(self.operation_dock_upload)
        return dock

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
        self.description_input = QPlainTextEdit()
        self.description_input.setObjectName("douyinCommerceDescription")
        self.description_input.setPlaceholderText("填写视频发布文案。上传后会由平台编辑页回读。")
        self.description_input.setFixedHeight(154)
        self.description_input.textChanged.connect(self._content_changed)
        content_layout.addWidget(self.description_input)
        tag_title_row = QHBoxLayout()
        tag_title = QLabel("话题标签（选填）")
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
        history_title = QLabel("最近使用标签")
        history_title.setObjectName("douyinCommerceFieldLabel")
        content_layout.addWidget(history_title)
        self.tag_history_host = QFrame()
        self.tag_history_host.setObjectName("douyinCommerceTagHistoryHost")
        self.tag_history_layout = QHBoxLayout(self.tag_history_host)
        self.tag_history_layout.setContentsMargins(8, 6, 8, 6)
        self.tag_history_layout.setSpacing(6)
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
        content_layout.addWidget(self._stage_error_label("content"))
        columns.addWidget(content_panel, 0, 1)

        self.content_execution_log = ExecutionLogPanel()
        self.content_execution_log.setMinimumHeight(510)
        columns.addWidget(self.content_execution_log, 0, 2)
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

        session_bar = QFrame()
        session_bar.setObjectName("douyinCommercePlatformProgress")
        session_layout = QHBoxLayout(session_bar)
        session_layout.setContentsMargins(14, 10, 14, 10)
        session_layout.setSpacing(10)
        self.platform_session_status = QLabel("等待上传视频")
        self.platform_session_status.setObjectName("douyinCommercePlatformProgressText")
        session_layout.addWidget(self.platform_session_status, 1)
        self.platform_back_button = button("返回内容", variant="secondary", compact=True)
        self.platform_back_button.setObjectName("douyinCommerceBackToContent")
        self.platform_back_button.clicked.connect(lambda: self._go_to_step(0))
        layout.addWidget(session_bar)

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
        left_layout.addWidget(self.music_stage)
        left_layout.addWidget(self.declaration_stage)
        left_layout.addStretch(1)
        columns.addWidget(self.platform_left_column, 0, 0)

        self.platform_right_column = QFrame()
        self.platform_right_column.setObjectName("douyinCommercePlatformRightColumn")
        right_layout = QVBoxLayout(self.platform_right_column)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(14)
        self.location_stage = self._build_location_stage()
        self.schedule_stage = self._build_schedule_stage()
        right_layout.addWidget(self.location_stage)
        right_layout.addWidget(self.schedule_stage)
        right_layout.addStretch(1)
        columns.addWidget(self.platform_right_column, 0, 1)

        self.platform_execution_log = ExecutionLogPanel()
        columns.addWidget(self.platform_execution_log, 0, 2)

        columns.setColumnStretch(0, 31)
        columns.setColumnStretch(1, 42)
        columns.setColumnStretch(2, 27)
        columns.setColumnMinimumWidth(0, 300)
        columns.setColumnMinimumWidth(1, 300)
        columns.setColumnMinimumWidth(2, 300)
        self.platform_execution_log.setMinimumWidth(300)
        layout.addWidget(workspace)

        self.platform_review_dock = QFrame()
        self.platform_review_dock.setObjectName("douyinCommercePlatformReviewDock")
        review_layout = QHBoxLayout(self.platform_review_dock)
        review_layout.setContentsMargins(16, 12, 16, 12)
        review_layout.setSpacing(12)
        self.platform_review_status = QLabel("完成音乐、地点和声明后可检查")
        self.platform_review_status.setObjectName("douyinCommercePlatformReviewStatus")
        self.platform_review_status.setWordWrap(True)
        review_layout.addWidget(self.platform_review_status, 1)
        review_layout.addWidget(self.platform_back_button)
        self.to_review_button = button("检查并继续", variant="primary")
        self.to_review_button.setObjectName("douyinCommerceToReview")
        self.to_review_button.clicked.connect(self.continue_to_review)
        review_layout.addWidget(self.to_review_button)
        layout.addWidget(self.platform_review_dock)
        return self._scroll_page(body)

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
            "直接选择后写入平台。",
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
            "选择后写入平台。",
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
        self.schedule_date = QDateEdit()
        self.schedule_date.setObjectName("douyinCommerceScheduleDate")
        self.schedule_date.setCalendarPopup(True)
        tomorrow = datetime.now(_SHANGHAI_TZ).date() + timedelta(days=1)
        self.schedule_date.setDate(QDate(tomorrow.year, tomorrow.month, tomorrow.day))
        self.schedule_date.dateChanged.connect(self._schedule_changed)
        self.schedule_time = QTimeEdit()
        self.schedule_time.setObjectName("douyinCommerceScheduleTime")
        self.schedule_time.setDisplayFormat("HH:mm")
        self.schedule_time.setTime(QTime(9, 0))
        self.schedule_time.timeChanged.connect(self._schedule_changed)
        schedule_row.addWidget(QLabel("北京时间"))
        schedule_row.addWidget(self.schedule_date)
        schedule_row.addWidget(self.schedule_time)
        panel_layout.addLayout(schedule_row)
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
            "先做只填写与回读的预检。预检通过不代表已定时或已发布；"
            "只有最终提交后收到平台管理页回执，任务才会记录为已定时或已发布。",
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
        self.validation_label = QLabel("完成作品内容声明后可开始预检；如开启定时，还需设置未来时间")
        self.validation_label.setObjectName("douyinCommerceReviewValidation")
        self.validation_label.setWordWrap(True)
        panel_layout.addWidget(self.validation_label)
        actions = QHBoxLayout()
        self.preflight_button = button("执行发布前检查", variant="primary")
        self.preflight_button.setObjectName("douyinCommercePreflight")
        self.preflight_button.clicked.connect(self.start_preflight)
        self.submit_button = button("确认立即发表", variant="secondary")
        self.submit_button.setObjectName("douyinCommerceSubmit")
        self.submit_button.clicked.connect(self.open_submit_confirmation)
        actions.addWidget(self.preflight_button)
        actions.addWidget(self.submit_button)
        actions.addStretch()
        panel_layout.addLayout(actions)

        review_columns.addWidget(panel, 0, 0, Qt.AlignmentFlag.AlignTop)
        self.review_execution_log = ExecutionLogPanel()
        review_columns.addWidget(self.review_execution_log, 0, 1)
        review_columns.setColumnStretch(0, 73)
        review_columns.setColumnStretch(1, 27)
        review_columns.setColumnMinimumWidth(1, 300)
        self.review_execution_log.setMinimumWidth(300)
        layout.addWidget(review_workspace)

        navigation = QHBoxLayout()
        self.review_back_button = button("返回平台设置", variant="secondary")
        self.review_back_button.clicked.connect(self.return_from_review)
        navigation.addWidget(self.review_back_button)
        navigation.addStretch()
        layout.addLayout(navigation)
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
        self._refresh_saved_content_status()
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
        """回到最近一次平台确认的音乐，避免失败后保留客户端暂选。"""

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
            self.music_combo.setCurrentIndex(index)
        finally:
            self.music_combo.blockSignals(False)

    def _clear_music_candidates(self) -> None:
        """清空瞬态收藏列表，不让旧会话的候选流入新上传会话。"""

        self._music_candidates = []
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
                chip = button(f"+ #{tag}", variant="ghost", compact=True)
                chip.setObjectName("douyinCommerceTagHistoryChip")
                chip.setToolTip(f"添加历史标签 #{tag}")
                chip.clicked.connect(
                    lambda _checked=False, value=tag: self.add_history_tag(value)
                )
                self.tag_history_layout.addWidget(chip)
        self.tag_history_layout.addStretch(1)

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
        self._uploaded_editor_payload = None
        self._pending_upload_payload = None
        self._preflight_fingerprint = ""
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

    def _schedule_changed(self) -> None:
        self._preflight_fingerprint = ""
        self._sync_view()

    def _timer_enabled_changed(self, _checked: bool) -> None:
        """切换发布方式只影响本次预检指纹，不触发任何平台动作。"""

        self._preflight_fingerprint = ""
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
        self._start_music_write(dict(candidate))

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
        self._start_music_write(dict(data))

    def _declaration_toggled(self, declaration: str, checked: bool) -> None:
        """用户切换声明时立即写入；初始化与失败恢复不触发平台动作。"""

        if not checked or self._suppress_declaration_signal:
            return
        self._start_declaration_write(declaration)

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
            kind == "music_read"
            and self._open_music_picker_after_load
            and self._music_candidates
        ):
            self._open_music_picker_after_load = False
            QTimer.singleShot(0, self._open_music_picker_if_ready)

    def _open_music_picker_if_ready(self) -> None:
        """仅在读取完成且控件恢复可用后展开同一音乐下拉框。"""

        if self.music_combo.isEnabled() and self._music_candidates:
            self.music_combo.showPopup()

    def _sync_view(self) -> None:
        """兼容既有信号/回调入口，统一委托给工作台状态投影。"""

        self._sync_workbench()

    def _sync_workbench(self) -> None:
        current_step = self.pages.currentIndex()
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
            has_editor_session = bool(self._session_id)
            self.review_back_button.setText(
                "返回平台设置" if has_editor_session else "开始新内容"
            )
            self.review_back_button.setEnabled(not self._busy())
        self._sync_content_cards()

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
        self.platform_stage_button.setEnabled(bool(self._session_id) and not self._busy())
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
        self.abandon_button.setEnabled(bool(self._session_id) and not self._busy())
        self.abandon_button.setVisible(bool(self._session_id))
        self.save_content_button.setEnabled(not self._busy())
        self.restore_content_button.setEnabled(
            self._saved_content_available and not self._session_id and not self._busy()
        )
        self.clear_content_button.setEnabled(not self._session_id and not self._busy())

        self._sync_platform_workspace()
        summary = self._summary()
        for key, value in self.summary_values.items():
            value.setText(summary.get(key) or "待完成")
        try:
            self.collect_payload("preflight")
            valid = self._can_review()
            if valid:
                validation = (
                    "可开始预检：仅回读当前编辑页字段与北京时间定时，不会保存草稿或提交发布。"
                    if self.timer_enabled.isChecked()
                    else "可开始预检：仅回读当前编辑页字段与立即发表状态，不会保存草稿或发表。"
                )
            else:
                validation = (
                    "请先完成作品内容声明与未来定时。"
                    if self.timer_enabled.isChecked()
                    else "请先完成作品内容声明。"
                )
        except (ValueError, douyin_commerce_service.DouyinCommerceError) as exc:
            valid = False
            validation = str(exc)
        if self._session_id and self._content_change_kind() != "none":
            valid = False
            validation = "内容已变更，请先同步内容或重新上传后再继续。"
        self.validation_label.setText(validation)
        preflight_ready = valid and bool(self._session_id) and not self._busy()
        fingerprint = self._payload_fingerprint("preflight") if preflight_ready else ""
        submit_ready = bool(
            preflight_ready
            and fingerprint
            and fingerprint == self._preflight_fingerprint
            and not self._busy()
        )
        self.preflight_button.setEnabled(preflight_ready)
        self.submit_button.setEnabled(submit_ready)
        self.submit_button.setText("确认定时提交" if self.timer_enabled.isChecked() else "确认立即发表")
        self._set_button_variant(
            self.preflight_button,
            "primary" if preflight_ready and not submit_ready else "secondary",
        )
        self._set_button_variant(self.submit_button, "primary" if submit_ready else "secondary")

        if self._busy():
            self.status_badge.setText("正在处理")
        elif submit_ready:
            self.status_badge.setText("预检已通过")
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
        session_ready = bool(self._session_id) and self._content_change_kind() == "none"
        self.platform_back_button.setEnabled(not busy)
        if self._session_id and self._content_change_kind() == "sync":
            self.platform_session_status.setText("内容已修改，请返回内容同步")
        elif self._session_id and self._content_change_kind() == "reupload":
            self.platform_session_status.setText("账号或视频已变更，请重新上传")
        elif busy:
            self.platform_session_status.setText("正在处理，请稍候")
        elif session_ready:
            self.platform_session_status.setText("编辑会话已就绪")
        else:
            self.platform_session_status.setText("等待上传视频")
        self.platform_session_status.setVisible(
            busy or (bool(self._session_id) and not session_ready)
        )

        can_choose_music = session_ready and not busy
        self.music_combo.setEnabled(can_choose_music)
        if not busy:
            if not session_ready:
                self.music_status.setText("上传后选择")
            elif self._selected_music:
                self.music_status.setText("")
            elif self._music_candidates:
                self.music_status.setText("直接选择一首收藏音乐")
            else:
                self.music_status.setText("打开下拉读取收藏音乐")
        self.music_status.setVisible(
            self._immediate_write_kind in {"music", "music_read"}
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

        can_choose_declaration = session_ready and not busy
        for option in self.declaration_buttons.values():
            option.setEnabled(can_choose_declaration)
        if not busy:
            if not can_choose_declaration:
                self.declaration_status.setText("上传后选择")
            elif self._declaration_applied:
                self.declaration_status.setText("")
            else:
                self.declaration_status.setText("选择后立即写入")
        self.declaration_status.setVisible(self._immediate_write_kind == "declaration")
        self.declaration_card.setVisible(False)

        can_configure_schedule = session_ready and not busy
        self.timer_enabled.setEnabled(can_configure_schedule)
        self.schedule_date.setEnabled(
            can_configure_schedule and self.timer_enabled.isChecked()
        )
        self.schedule_time.setEnabled(
            can_configure_schedule and self.timer_enabled.isChecked()
        )
        if not can_configure_schedule:
            self.publish_mode_hint.setText("上传后设置")
        elif self.timer_enabled.isChecked():
            self.publish_mode_hint.setText("定时：北京时间")
        else:
            self.publish_mode_hint.setText("立即发表")

        review_ready = self._can_review() and not busy
        self.to_review_button.setEnabled(review_ready)
        self._set_button_variant(
            self.to_review_button, "primary" if review_ready else "secondary"
        )
        checking_schedule = self._immediate_write_kind == "schedule"
        self.to_review_button.setText("正在检查…" if checking_schedule else "检查并继续")
        if checking_schedule:
            self.platform_review_status.setText("正在检查发布时间…")
        elif review_ready:
            self.platform_review_status.setText("已完成，可检查")
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
            return bool(self._session_id) and self._content_change_kind() == "none"
        if step == 1:
            return self._can_review()
        return bool(self._preflight_fingerprint)

    def _busy(self) -> bool:
        return bool(self._immediate_write_kind) or any(
            self.runner.is_running(key)
            for key in (
                "douyin_commerce_upload",
                "douyin_commerce_sync_content",
                self._IMMEDIATE_WRITE_KEY,
                "douyin_commerce_preflight",
                "douyin_commerce_submit",
            )
        )

    def _content_is_valid(self) -> bool:
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
            "title": self.title_input.text().strip(),
            "description": self.description_input.toPlainText().strip(),
            "tags": self._tags(),
            "fileList": [str(video.get("storedPath") or "")],
            "musicMode": "favorite-manual",
            "accountList": [str(account.get("filePath") or "")],
            "enableTimer": False,
            "runtimeMode": "preflight",
            "debugDryRun": True,
            "backgroundMode": True,
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
            "backgroundMode": True,
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

    def continue_after_content(self) -> None:
        """按内容差异决定无动作、内容同步或完整重新上传。"""

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

    def _clear_commerce_progress(self) -> None:
        self._commerce_progress_phase = ""
        self.operation_progress.setValue(0)
        self.operation_progress_frame.setVisible(False)

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
        self._preflight_fingerprint = ""
        self._uploaded_editor_payload = dict(self._pending_upload_payload or {}) or None
        self._pending_upload_payload = None
        self.content_notice.setText("视频已上传一次；后续设置不会重复上传。")
        self.content_notice.setVisible(False)
        self._go_to_step(1)
        self._start_declaration_write(self._DEFAULT_CONTENT_DECLARATION)

    def _load_favorite_music_candidates(self) -> None:
        """仅在用户打开下拉框时读取收藏列表，不用音乐操作锁住其他设置。"""

        if not self._session_id:
            QMessageBox.warning(self, "选择收藏音乐", "请先上传视频。")
            return
        if self._music_candidates:
            self.music_combo.showPopup()
            return
        session_id = self._session_id
        self.music_status.setText("正在读取收藏音乐…")
        self._open_music_picker_after_load = self._start_immediate_write(
            "music_read",
            lambda: douyin_commerce_session.commerce_session_manager.load_favorite_music(
                session_id
            ),
            self._show_music_candidates,
            self._music_load_failed,
        )

    def _show_music_candidates(self, rows: list[dict[str, str]]) -> None:
        self._music_candidates = [dict(item) for item in rows]
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
        self.music_status.setText(f"已读取 {len(self._music_candidates)} 首收藏音乐，直接选择即可。")
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
        self.music_status.setText("正在将用户所选音乐写入抖音编辑页并回读…")
        session_id = self._session_id
        self._start_immediate_write(
            "music",
            lambda: douyin_commerce_session.commerce_session_manager.select_favorite_music(
                session_id, music_id
            ),
            self._music_selected,
            self._immediate_music_failed,
        )

    def _music_selected(self, music: dict[str, str]) -> None:
        self._selected_music = dict(music)
        # musicId 对应的 marker 只在刚关闭的抖音弹窗中有效；保留会导致下次更换
        # 时在客户端看似选中了新音乐，服务端却无法确认该条目。
        self._discard_music_candidates_for_reload()
        self._clear_stage_error("music")
        self.music_card.setText(self._music_display(self._selected_music))
        self.music_status.setText("")
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
        if not self._session_id:
            QMessageBox.warning(self, "搜索发布定位", "请先完成视频上传。")
            return
        if not scope:
            QMessageBox.warning(self, "搜索发布定位", "请先选择地点范围：本地或国内。")
            return
        self.location_status.setText(
            f"正在按“{douyin_commerce_service.location_scope_label(scope)}”范围，从当前抖音编辑页读取候选…"
        )
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
        action_label = "确认定时提交" if self.timer_enabled.isChecked() else "确认立即发表"
        try:
            payload = self.collect_payload("publish")
        except Exception as exc:
            QMessageBox.warning(self, action_label, str(exc))
            self._sync_view()
            return
        if self._payload_fingerprint("preflight") != self._preflight_fingerprint:
            QMessageBox.warning(
                self,
                "需要重新预检",
                "账号、素材、内容、音乐、地点、作品内容声明或定时已变更，请先重新完成预检。",
            )
            return
        dialog = DouyinCommerceConfirmDialog(
            self._summary(),
            enable_timer=payload.get("enableTimer") is True,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
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

        if self._active_task_id is None:
            return
        if not self._douyin_verification_poll_timer.isActive():
            self._douyin_verification_poll_timer.start()
        self._poll_douyin_verification()

    def _poll_douyin_verification(self) -> None:
        """按任务号打开唯一原生验证对话框，不执行浏览器操作。"""

        if self._active_task_id is None:
            return
        request_id = verification_broker.request_for_task(self._active_task_id)
        if not request_id or request_id == self._douyin_verification_request_id:
            return
        if self._douyin_verification_dialog is not None:
            self._douyin_verification_dialog.accept()
        dialog = DouyinVerificationDialog(
            request_id,
            broker=verification_broker,
            parent=self,
        )
        self._douyin_verification_dialog = dialog
        self._douyin_verification_request_id = request_id
        dialog.show()

    def _cleanup_douyin_verification(self) -> None:
        """任务收束时停止轮询、关闭对话框并清空对应内存请求。"""

        self._douyin_verification_poll_timer.stop()
        request_id = self._douyin_verification_request_id
        if not request_id and self._active_task_id is not None:
            request_id = verification_broker.request_for_task(self._active_task_id) or ""
        if self._douyin_verification_dialog is not None:
            self._douyin_verification_dialog.accept()
        if request_id:
            verification_broker.clear(request_id)
        self._douyin_verification_dialog = None
        self._douyin_verification_request_id = ""

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
        if not self._session_id:
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
        self._session_id = ""
        if session_id:
            douyin_commerce_session.commerce_session_manager.close(session_id)
        self._clear_music_candidates()
        self._selected_music = None
        self.music_card.setText("尚未选择收藏音乐")
        self.music_status.setText("本次上传会话已结束")
        self._locations = []
        self._selected_location_data = None
        self._pending_location = None
        self.location_result_list.clear()
        self.location_candidate_card.setText("尚未选择发布定位")
        self.location_applied_card.setText("尚未选择发布定位")
        self._location_applied = False
        self.location_scope_combo.blockSignals(True)
        self.location_scope_combo.setCurrentIndex(2)
        self.location_scope_combo.blockSignals(False)
        self._clear_declaration("请重新上传视频后选择地点与作品内容声明")
        self._preflight_fingerprint = ""
        self._uploaded_editor_payload = None
        self._pending_upload_payload = None
        # 放弃只影响临时会话；从本机重新读取保存状态，确保恢复入口立即回到正确状态。
        self._refresh_saved_content_status()
        self._sync_view()
        if not silent:
            QMessageBox.information(self, "抖音带货", "已关闭临时编辑页，未保存草稿或发布。")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 固定事件名
        if self._session_id and not self._busy():
            self._abandon_session(silent=True)
        super().closeEvent(event)
