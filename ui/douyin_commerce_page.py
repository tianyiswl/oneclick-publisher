# -*- coding: utf-8 -*-
"""抖音带货的分步桌面工作流。

抖音的音乐、位置和作品内容声明只能在视频上传后的编辑页完成。本页因此不是
把所有字段堆在一个表单，而是明确分为：内容准备 → 用户选择音乐与地点范围 →
按平台实际路径选择发布定位 → 选择自主声明与定时 → 预检与提交。一次上传对应
一个仅内存中的受控编辑会话；不保存草稿，也不会在没有最终确认时点击发表。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from PyQt6.QtCore import QDate, QSize, QTime, Qt
from PyQt6.QtWidgets import (
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
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from app_core import (
    account_service,
    douyin_commerce_draft_service,
    douyin_commerce_service,
    douyin_commerce_session,
    media_service,
    task_service,
)

from .background_task import BackgroundTaskRunner
from .common import button


_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


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

    _STEPS = ("内容准备", "音乐与定位", "声明与定时", "检查与提交")

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("pageRoot")
        self.runner = BackgroundTaskRunner(self)
        self._session_id = ""
        self._music_candidates: list[dict[str, str]] = []
        self._selected_music: dict[str, str] | None = None
        self._locations: list[dict[str, str]] = []
        self._location_applied = False
        self._declaration_applied = False
        self._preflight_fingerprint = ""
        self._active_task_id: int | None = None
        self._content_dirty_after_upload = False
        self._saved_content_available = False
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 28, 26)
        root.setSpacing(16)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(4)
        title = QLabel("抖音带货")
        title.setObjectName("pageTitle")
        subtitle = QLabel("本地团购视频 · 用户自选收藏音乐 · 发布定位与自主声明 · 可选北京时间定时")
        subtitle.setObjectName("pageSubtitle")
        subtitle.setWordWrap(True)
        titles.addWidget(title)
        titles.addWidget(subtitle)
        header.addLayout(titles)
        header.addStretch()
        self.status_badge = QLabel("等待内容准备")
        self.status_badge.setProperty("role", "countBadge")
        header.addWidget(self.status_badge)
        self.abandon_button = button("放弃本次上传", variant="secondary")
        self.abandon_button.setObjectName("douyinCommerceAbandon")
        self.abandon_button.clicked.connect(self.abandon_session)
        header.addWidget(self.abandon_button)
        root.addLayout(header)

        guidance = QLabel(
            "先上传一次视频；上传完成后由你从收藏中选择音乐，再在默认“国内”范围下搜索，或切换为“本地”，"
            "按抖音实际的“位置 → 带货模式”路径确认发布定位。随后选择作品内容声明并回读。"
            "本流程不读取或绑定门店、不设置独立封面、不保存草稿，最终提交始终需要单独确认。"
        )
        guidance.setObjectName("infoCallout")
        guidance.setWordWrap(True)
        root.addWidget(guidance)

        self.step_row = QHBoxLayout()
        self.step_row.setSpacing(8)
        self.step_labels: list[QLabel] = []
        for index, label_text in enumerate(self._STEPS):
            label = QLabel(f"{index + 1}. {label_text}")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setMinimumHeight(34)
            label.setProperty("role", "caption")
            self.step_row.addWidget(label, 1)
            self.step_labels.append(label)
        root.addLayout(self.step_row)

        self.pages = QStackedWidget()
        self.pages.setObjectName("douyinCommerceWizard")
        self.pages.addWidget(self._build_content_page())
        self.pages.addWidget(self._build_music_location_page())
        self.pages.addWidget(self._build_store_schedule_page())
        self.pages.addWidget(self._build_review_page())
        root.addWidget(self.pages, 1)

    @staticmethod
    def _scroll_page(body: QWidget) -> QScrollArea:
        scroll = QScrollArea()
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

    def _build_content_page(self) -> QWidget:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 12, 0)
        layout.setSpacing(14)

        account_panel, account_layout = self._section(
            "1. 选择抖音账号",
            "仅显示账号管理中状态正常的抖音账号；一次任务只能使用一个账号。",
        )
        row = QHBoxLayout()
        label = QLabel("发布账号")
        label.setMinimumWidth(74)
        self.account_combo = QComboBox()
        self.account_combo.setObjectName("douyinCommerceAccount")
        self.account_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.account_combo.currentIndexChanged.connect(self._content_changed)
        row.addWidget(label)
        row.addWidget(self.account_combo, 1)
        account_layout.addLayout(row)
        layout.addWidget(account_panel)

        content_panel, content_layout = self._section(
            "2. 视频与文案",
            "首版只支持一个本地视频。抖音带货不设置独立封面；上传后由你选择收藏音乐。",
        )
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        grid.addWidget(QLabel("视频素材"), 0, 0)
        self.video_combo = QComboBox()
        self.video_combo.setObjectName("douyinCommerceVideo")
        self.video_combo.currentIndexChanged.connect(self._content_changed)
        grid.addWidget(self.video_combo, 0, 1)
        grid.addWidget(QLabel("标题"), 1, 0)
        self.title_input = QLineEdit()
        self.title_input.setObjectName("douyinCommerceTitle")
        self.title_input.setMaxLength(55)
        self.title_input.setPlaceholderText("填写抖音视频标题")
        self.title_input.textChanged.connect(self._content_changed)
        grid.addWidget(self.title_input, 1, 1)
        grid.addWidget(QLabel("话题标签"), 2, 0)
        self.tags_input = QLineEdit()
        self.tags_input.setObjectName("douyinCommerceTags")
        self.tags_input.setPlaceholderText("例如：本地团购，门店推荐（用空格或逗号分隔）")
        self.tags_input.textChanged.connect(self._content_changed)
        grid.addWidget(self.tags_input, 2, 1)
        grid.setColumnStretch(1, 1)
        content_layout.addLayout(grid)
        desc_label = QLabel("作品文案")
        content_layout.addWidget(desc_label)
        self.description_input = QPlainTextEdit()
        self.description_input.setObjectName("douyinCommerceDescription")
        self.description_input.setPlaceholderText("填写视频发布文案。上传后会由平台编辑页回读。")
        self.description_input.setFixedHeight(130)
        self.description_input.textChanged.connect(self._content_changed)
        content_layout.addWidget(self.description_input)
        self.content_notice = QLabel("填写完成后上传一次视频，后续步骤不会重复上传。")
        self.content_notice.setProperty("role", "caption")
        self.content_notice.setWordWrap(True)
        content_layout.addWidget(self.content_notice)
        saved_content_row = QHBoxLayout()
        self.save_content_button = button("保存内容", variant="secondary")
        self.save_content_button.setObjectName("douyinCommerceSaveContent")
        self.save_content_button.clicked.connect(self.save_content)
        self.restore_content_button = button("恢复已保存内容", variant="secondary")
        self.restore_content_button.setObjectName("douyinCommerceRestoreContent")
        self.restore_content_button.clicked.connect(self.restore_saved_content)
        self.content_save_status = QLabel("尚未保存本地内容")
        self.content_save_status.setObjectName("douyinCommerceContentSaveStatus")
        self.content_save_status.setProperty("role", "caption")
        self.content_save_status.setWordWrap(True)
        saved_content_row.addWidget(self.save_content_button)
        saved_content_row.addWidget(self.restore_content_button)
        saved_content_row.addWidget(self.content_save_status, 1)
        content_layout.addLayout(saved_content_row)
        layout.addWidget(content_panel)

        actions = QHBoxLayout()
        actions.addStretch()
        self.upload_button = button("上传并继续", variant="primary")
        self.upload_button.setObjectName("douyinCommerceUpload")
        self.upload_button.clicked.connect(self.start_upload)
        actions.addWidget(self.upload_button)
        layout.addLayout(actions)
        layout.addStretch()
        return self._scroll_page(body)

    def _build_music_location_page(self) -> QWidget:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 12, 0)
        layout.setSpacing(14)

        music_panel, music_layout = self._section(
            "3. 选择收藏音乐",
            "点击后只读取当前抖音账号在编辑页可见的“收藏”列表。不会默认选择第一首，"
            "也不会搜索或填写任意音乐名。",
        )
        music_actions = QHBoxLayout()
        self.load_music_button = button("读取收藏音乐", variant="primary")
        self.load_music_button.setObjectName("douyinCommerceLoadMusic")
        self.load_music_button.clicked.connect(self.load_music)
        self.use_music_button = button("使用所选音乐", variant="secondary")
        self.use_music_button.setObjectName("douyinCommerceUseMusic")
        self.use_music_button.clicked.connect(self.use_selected_music)
        self.music_status = QLabel("视频上传完成后可读取收藏音乐")
        self.music_status.setProperty("role", "caption")
        self.music_status.setWordWrap(True)
        music_actions.addWidget(self.load_music_button)
        music_actions.addWidget(self.use_music_button)
        music_actions.addWidget(self.music_status, 1)
        music_layout.addLayout(music_actions)
        self.music_list = QListWidget()
        self.music_list.setObjectName("douyinCommerceMusicCandidates")
        self.music_list.setMinimumHeight(170)
        self.music_list.itemSelectionChanged.connect(self._music_candidate_changed)
        music_layout.addWidget(self.music_list)
        self.music_card = self._selection_card("尚未选择收藏音乐")
        self.music_card.setObjectName("douyinCommerceMusicCard")
        music_layout.addWidget(self.music_card)
        layout.addWidget(music_panel)

        location_panel, location_layout = self._section(
            "4. 选择发布定位",
            "地点范围默认“国内”，可切换为“本地”；再按平台实际顺序：位置 → 带货模式 → "
            "输入地点。候选会显示完整地址；本步骤只选择并回读发布定位，不读取或绑定门店。",
        )
        search_row = QHBoxLayout()
        self.location_scope_combo = QComboBox()
        self.location_scope_combo.setObjectName("douyinCommerceLocationScope")
        self.location_scope_combo.addItem("选择范围：本地或国内", "")
        for scope in (
            douyin_commerce_service.LOCATION_SCOPE_LOCAL,
            douyin_commerce_service.LOCATION_SCOPE_DOMESTIC,
        ):
            self.location_scope_combo.addItem(
                douyin_commerce_service.location_scope_label(scope), scope
            )
        self.location_scope_combo.currentIndexChanged.connect(self._location_scope_changed)
        # 抖音带货更常见的是跨城市/跨区域检索；保留“本地”可切换，但新会话默认
        # 显示“国内”，避免用户每次都从占位项重新选择。
        # 构建阶段地点结果列表尚未创建，初始赋值不能触发清理回调。
        self.location_scope_combo.blockSignals(True)
        self.location_scope_combo.setCurrentIndex(2)
        self.location_scope_combo.blockSignals(False)
        self.location_keyword = QLineEdit()
        self.location_keyword.setObjectName("douyinCommerceLocationKeyword")
        self.location_keyword.setPlaceholderText("输入地点，例如：北海夜南香")
        self.location_keyword.returnPressed.connect(self.search_locations)
        self.location_keyword.textEdited.connect(self._location_keyword_edited)
        self.location_search_button = button("搜索地点", variant="secondary")
        self.location_search_button.clicked.connect(self.search_locations)
        search_row.addWidget(self.location_scope_combo)
        search_row.addWidget(self.location_keyword, 1)
        search_row.addWidget(self.location_search_button)
        location_layout.addLayout(search_row)
        self.location_status = QLabel("完成音乐选择后，可从当前抖音编辑页搜索发布定位（默认范围：国内）")
        self.location_status.setObjectName("douyinCommerceLocationStatus")
        self.location_status.setProperty("role", "caption")
        self.location_status.setWordWrap(True)
        location_layout.addWidget(self.location_status)
        self.location_result_list = QListWidget()
        self.location_result_list.setObjectName("douyinCommerceLocationResults")
        self.location_result_list.setMinimumHeight(260)
        self.location_result_list.setWordWrap(True)
        self.location_result_list.setUniformItemSizes(False)
        self.location_result_list.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.location_result_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.location_result_list.itemSelectionChanged.connect(self._location_selected)
        location_layout.addWidget(self.location_result_list)
        self.location_card = self._selection_card("尚未选择发布定位")
        self.location_card.setObjectName("douyinCommerceLocationCard")
        location_layout.addWidget(self.location_card)
        location_actions = QHBoxLayout()
        location_actions.addStretch()
        self.apply_location_button = button("使用所选发布定位", variant="secondary")
        self.apply_location_button.setObjectName("douyinCommerceApplyLocation")
        self.apply_location_button.clicked.connect(self.apply_selected_location)
        location_actions.addWidget(self.apply_location_button)
        location_layout.addLayout(location_actions)
        layout.addWidget(location_panel)

        navigation = QHBoxLayout()
        back = button("返回内容准备", variant="secondary")
        back.clicked.connect(lambda: self._go_to_step(0))
        self.to_declaration_button = button("继续到声明与定时", variant="secondary")
        self.to_declaration_button.setObjectName("douyinCommerceToDeclaration")
        self.to_declaration_button.clicked.connect(lambda: self._go_to_step(2))
        navigation.addWidget(back)
        navigation.addStretch()
        navigation.addWidget(self.to_declaration_button)
        layout.addLayout(navigation)
        return self._scroll_page(body)

    def _build_store_schedule_page(self) -> QWidget:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 12, 0)
        layout.setSpacing(14)

        declaration_panel, declaration_layout = self._section(
            "5. 作品内容声明（自主声明）",
            "从抖音当前页面的真实声明选项中选择一项。不会根据素材、标题或 AI 字样自动猜测；"
            "选择后必须由编辑页回读确认。",
        )
        declaration_row = QHBoxLayout()
        self.declaration_combo = QComboBox()
        self.declaration_combo.setObjectName("douyinCommerceContentDeclaration")
        self.declaration_combo.addItem("请选择作品内容声明", "")
        for declaration in douyin_commerce_service.CONTENT_DECLARATION_OPTIONS:
            self.declaration_combo.addItem(declaration, declaration)
        self.declaration_combo.currentIndexChanged.connect(self._declaration_changed)
        self.apply_declaration_button = button("写入所选声明", variant="secondary")
        self.apply_declaration_button.setObjectName("douyinCommerceApplyDeclaration")
        self.apply_declaration_button.clicked.connect(self.apply_selected_declaration)
        declaration_row.addWidget(self.declaration_combo, 1)
        declaration_row.addWidget(self.apply_declaration_button)
        declaration_layout.addLayout(declaration_row)
        self.declaration_status = QLabel("先在上一步确认发布定位")
        self.declaration_status.setProperty("role", "caption")
        self.declaration_status.setWordWrap(True)
        declaration_layout.addWidget(self.declaration_status)
        self.declaration_card = self._selection_card("尚未选择作品内容声明")
        self.declaration_card.setObjectName("douyinCommerceDeclarationCard")
        declaration_layout.addWidget(self.declaration_card)
        layout.addWidget(declaration_panel)

        schedule_panel, schedule_layout = self._section(
            "6. 发布方式", "可立即发表，或开启定时发表；定时按 Asia/Shanghai（北京时间）校验且必须是未来时间。"
        )
        self.timer_enabled = QCheckBox("开启定时发表")
        self.timer_enabled.setChecked(False)
        self.timer_enabled.toggled.connect(self._timer_enabled_changed)
        schedule_layout.addWidget(self.timer_enabled)
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
        schedule_row.addStretch()
        schedule_layout.addLayout(schedule_row)
        layout.addWidget(schedule_panel)

        navigation = QHBoxLayout()
        back = button("返回音乐与定位", variant="secondary")
        back.clicked.connect(lambda: self._go_to_step(1))
        self.to_review_button = button("进入检查", variant="secondary")
        self.to_review_button.setObjectName("douyinCommerceToReview")
        self.to_review_button.clicked.connect(lambda: self._go_to_step(3))
        navigation.addWidget(back)
        navigation.addStretch()
        navigation.addWidget(self.to_review_button)
        layout.addLayout(navigation)
        return self._scroll_page(body)

    def _build_review_page(self) -> QWidget:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 12, 0)
        layout.setSpacing(14)

        panel, panel_layout = self._section(
            "7. 检查并提交",
            "先进行只填写与回读的预检。预检通过只代表当前编辑页字段已回读，不代表平台已定时；"
            "只有最终提交后收到平台管理页回执，任务才会记为已定时。",
        )
        self.summary_grid = QGridLayout()
        self.summary_grid.setHorizontalSpacing(12)
        self.summary_grid.setVerticalSpacing(12)
        self.summary_values: dict[str, QLabel] = {}
        for row, (label_text, key) in enumerate(
            (
                ("账号", "account"),
                ("视频", "video"),
                ("标题", "title"),
                ("用户所选音乐", "music"),
                ("发布位置", "location"),
                ("作品内容声明", "declaration"),
                ("发布方式", "schedule"),
            )
        ):
            label = QLabel(label_text)
            label.setProperty("role", "caption")
            value = QLabel("待完成")
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.summary_grid.addWidget(label, row, 0, Qt.AlignmentFlag.AlignTop)
            self.summary_grid.addWidget(value, row, 1)
            self.summary_values[key] = value
        self.summary_grid.setColumnStretch(1, 1)
        panel_layout.addLayout(self.summary_grid)
        self.validation_label = QLabel("完成作品内容声明后可开始预检；如开启定时，还需设置未来时间")
        self.validation_label.setProperty("role", "caption")
        self.validation_label.setWordWrap(True)
        panel_layout.addWidget(self.validation_label)
        actions = QHBoxLayout()
        self.preflight_button = button("开始预检", variant="primary")
        self.preflight_button.setObjectName("douyinCommercePreflight")
        self.preflight_button.clicked.connect(self.start_preflight)
        self.submit_button = button("确认立即发表", variant="secondary")
        self.submit_button.setObjectName("douyinCommerceSubmit")
        self.submit_button.clicked.connect(self.open_submit_confirmation)
        actions.addWidget(self.preflight_button)
        actions.addWidget(self.submit_button)
        actions.addStretch()
        panel_layout.addLayout(actions)
        layout.addWidget(panel)

        navigation = QHBoxLayout()
        back = button("返回声明与定时", variant="secondary")
        back.clicked.connect(lambda: self._go_to_step(2))
        navigation.addWidget(back)
        navigation.addStretch()
        layout.addLayout(navigation)
        layout.addStretch()
        return self._scroll_page(body)

    @staticmethod
    def _selection_card(initial_text: str) -> QLabel:
        card = QLabel(initial_text)
        card.setProperty("role", "caption")
        card.setWordWrap(True)
        card.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        card.setStyleSheet(
            "padding: 11px 13px; background: #F8FAFB; border: 1px solid #DCE3EA;"
            "border-radius: 8px; color: #344054;"
        )
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
            name = _normalized(account.get("profileName") or account.get("userName"))
            if not name:
                continue
            subject = _normalized(account.get("userName"))
            display = name if not subject or subject == name else f"{name} · {subject}"
            self.account_combo.addItem(display, dict(account))
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
        selected = self.location_result_list.selectedItems()
        if not selected:
            return None
        data = selected[0].data(Qt.ItemDataRole.UserRole)
        return dict(data) if isinstance(data, dict) else None

    def _selected_location_scope(self) -> str:
        return str(self.location_scope_combo.currentData() or "")

    def _selected_declaration(self) -> str:
        return str(self.declaration_combo.currentData() or "")

    def _selected_music_candidate(self) -> dict[str, str] | None:
        selected = self.music_list.selectedItems()
        if not selected:
            return None
        data = selected[0].data(Qt.ItemDataRole.UserRole)
        return dict(data) if isinstance(data, dict) else None

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
                f"已保存到本机：{saved.get('updatedAt') or '时间未知'}；不上传、不保存平台草稿、不发布"
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
        self._saved_content_available = True
        self.content_save_status.setText(
            f"已保存到本机：{saved.get('updatedAt') or '刚刚'}；不上传、不保存平台草稿、不发布"
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
        self.tags_input.setText("，".join(str(item) for item in payload.get("tags") or []))
        self.tags_input.blockSignals(False)
        self.description_input.blockSignals(True)
        self.description_input.setPlainText(str(payload.get("description") or ""))
        self.description_input.blockSignals(False)

        missing: list[str] = []
        if not account_restored and (payload.get("accountId") or payload.get("accountFile")):
            missing.append("已保存的抖音账号当前不在“状态正常”的账号列表中")
        if not video_restored and (payload.get("mediaId") or payload.get("mediaPath")):
            missing.append("已保存的视频素材当前不在素材管理中或文件已不存在")
        self._content_dirty_after_upload = False
        self._preflight_fingerprint = ""
        self._refresh_saved_content_status()
        self.content_notice.setText(
            "已恢复本机保存的内容。请核对账号与视频后，再点击“上传并继续”；不会自动上传。"
        )
        if missing:
            QMessageBox.warning(
                self,
                "部分内容待重新选择",
                "标题、文案和话题已恢复。\n\n" + "\n".join(missing),
            )
        else:
            QMessageBox.information(
                self,
                "内容已恢复",
                "已恢复本机保存的内容。请核对后手动点击“上传并继续”；不会自动上传。",
            )
        self._sync_view()

    def _content_changed(self) -> None:
        if self._session_id:
            self._content_dirty_after_upload = True
            self.content_notice.setText(
                "账号、素材或文案已变更。请点击“重新上传并继续”；旧临时编辑页会被放弃，"
                "不会保存草稿。"
            )
        self._preflight_fingerprint = ""
        self._sync_view()

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
        self._location_applied = False
        self.location_status.setText("关键词已修改，请从当前抖音编辑页重新搜索发布定位")
        self.location_card.setText("位置搜索词已修改，请重新搜索并选择平台候选")
        self._clear_declaration("位置关键词已变更，请重新选择平台候选")
        self._preflight_fingerprint = ""
        self._sync_view()

    def _location_scope_changed(self) -> None:
        """范围变更必须使旧地点和声明失效，避免跨范围误复用。"""

        if self.location_result_list.selectedItems():
            self.location_result_list.blockSignals(True)
            self.location_result_list.clearSelection()
            self.location_result_list.blockSignals(False)
        self._locations = []
        self.location_result_list.clear()
        self._location_applied = False
        self.location_card.setText("地点范围已变更，请重新搜索并选择平台候选")
        scope = self._selected_location_scope()
        if scope:
            self.location_status.setText(
                f"已选择“{douyin_commerce_service.location_scope_label(scope)}”范围，请输入地点后搜索"
            )
        else:
            self.location_status.setText("请先选择地点范围：本地或国内")
        self._clear_declaration("地点范围已变更，请在确认地点后重新选择作品内容声明")
        self._preflight_fingerprint = ""
        self._sync_view()

    def _location_selected(self) -> None:
        location = self._selected_location()
        if not location:
            self.location_card.setText("尚未选择发布定位")
            self._location_applied = False
            self._clear_declaration("先选择发布定位")
        else:
            self.location_card.setText(self._location_display(location))
            self._location_applied = False
            self._clear_declaration("发布定位候选已选择，等待在当前抖音编辑页确认")
        self._preflight_fingerprint = ""
        self._sync_view()

    def _music_candidate_changed(self) -> None:
        self._sync_view()

    def _declaration_changed(self) -> None:
        self._declaration_applied = False
        selected = self._selected_declaration()
        self.declaration_card.setText(
            f"已选择“{selected}”，等待写入当前抖音编辑页并回读"
            if selected
            else "尚未选择作品内容声明"
        )
        self.declaration_status.setText(
            "请选择作品内容声明；不选择时不会自动推断。"
            if not selected
            else "已选择声明，点击“写入所选声明”后才会在平台页生效。"
        )
        self._preflight_fingerprint = ""
        self._sync_view()

    def _clear_declaration(self, message: str) -> None:
        self.declaration_combo.blockSignals(True)
        self.declaration_combo.setCurrentIndex(0)
        self.declaration_combo.blockSignals(False)
        self._declaration_applied = False
        self.declaration_card.setText("尚未选择作品内容声明")
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
            f"{music.get('creator') or '平台未返回作者'} · {music.get('duration') or ''}\n"
            "已由当前抖音编辑页回读确认"
        )

    def _go_to_step(self, step: int) -> None:
        if step == 1 and not self._session_id:
            QMessageBox.warning(self, "抖音带货", "请先完成内容准备并上传视频。")
            return
        if step == 2 and not self._location_applied:
            QMessageBox.warning(self, "抖音带货", "请先选择并回读发布定位。")
            return
        if step == 3 and not self._can_review():
            timer_hint = "并设置未来定时" if self.timer_enabled.isChecked() else ""
            QMessageBox.warning(self, "抖音带货", f"请先完成作品内容声明{timer_hint}。")
            return
        self.pages.setCurrentIndex(step)
        self._sync_view()

    def _set_button_variant(self, control, variant: str) -> None:
        if control.property("variant") == variant:
            return
        control.setProperty("variant", variant)
        control.style().unpolish(control)
        control.style().polish(control)

    def _sync_view(self) -> None:
        current_step = self.pages.currentIndex()
        for index, label in enumerate(self.step_labels):
            if index == current_step:
                label.setStyleSheet(
                    "background:#E8F3FF; color:#155EEF; border:1px solid #B2DDFF;"
                    "border-radius:8px; padding:6px 10px; font-weight:700;"
                )
            elif self._step_complete(index):
                label.setStyleSheet(
                    "background:#ECFDF3; color:#067647; border:1px solid #ABEFC6;"
                    "border-radius:8px; padding:6px 10px; font-weight:600;"
                )
            else:
                label.setStyleSheet(
                    "background:#F8FAFC; color:#667085; border:1px solid #EAECF0;"
                    "border-radius:8px; padding:6px 10px;"
                )

        upload_ready = self._content_is_valid()
        self.upload_button.setEnabled(upload_ready and not self._busy())
        self.upload_button.setText("重新上传并继续" if self._session_id else "上传并继续")
        self.abandon_button.setEnabled(bool(self._session_id) and not self._busy())
        self.save_content_button.setEnabled(not self._busy())
        self.restore_content_button.setEnabled(
            self._saved_content_available and not self._session_id and not self._busy()
        )

        can_load_music = bool(self._session_id) and not self._content_dirty_after_upload and not self._music_candidates and self._selected_music is None
        can_use_music = bool(self._selected_music_candidate()) and bool(self._music_candidates) and not self._busy()
        self.load_music_button.setEnabled(can_load_music and not self._busy())
        self.use_music_button.setEnabled(can_use_music)
        self._set_button_variant(self.load_music_button, "primary" if can_load_music else "secondary")
        self._set_button_variant(self.use_music_button, "primary" if can_use_music else "secondary")

        can_search_location = (
            bool(self._session_id)
            and self._selected_music is not None
            and not self._content_dirty_after_upload
            and not self._busy()
        )
        self.location_scope_combo.setEnabled(can_search_location)
        self.location_keyword.setEnabled(can_search_location)
        self.location_search_button.setEnabled(
            can_search_location and bool(self._selected_location_scope())
        )
        selected_location = self._selected_location()
        can_apply_location = (
            bool(selected_location)
            and bool(self._selected_location_scope())
            and self._selected_music is not None
            and bool(self._session_id)
            and not self._busy()
        )
        self.apply_location_button.setEnabled(can_apply_location)
        self._set_button_variant(self.apply_location_button, "primary" if can_apply_location else "secondary")
        self.to_declaration_button.setEnabled(self._location_applied and not self._busy())

        can_apply_declaration = (
            self._location_applied
            and bool(self._selected_declaration())
            and bool(self._session_id)
            and not self._busy()
        )
        self.declaration_combo.setEnabled(self._location_applied and not self._busy())
        self.apply_declaration_button.setEnabled(can_apply_declaration)
        self._set_button_variant(
            self.apply_declaration_button,
            "primary" if can_apply_declaration else "secondary",
        )
        can_configure_timer = not self._busy()
        self.timer_enabled.setEnabled(can_configure_timer)
        self.schedule_date.setEnabled(can_configure_timer and self.timer_enabled.isChecked())
        self.schedule_time.setEnabled(can_configure_timer and self.timer_enabled.isChecked())
        self.to_review_button.setEnabled(self._can_review() and not self._busy())

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
        if self._content_dirty_after_upload:
            valid = False
            validation = "内容已在上传后变更，请重新上传后再继续。"
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
        self._set_button_variant(self.preflight_button, "primary" if preflight_ready else "secondary")
        self._set_button_variant(self.submit_button, "primary" if submit_ready else "secondary")

        if self._busy():
            self.status_badge.setText("正在处理")
        elif submit_ready:
            self.status_badge.setText("预检已通过")
        elif self._session_id:
            self.status_badge.setText("继续配置")
        else:
            self.status_badge.setText("等待内容准备")

    def _step_complete(self, step: int) -> bool:
        if step == 0:
            return bool(self._session_id) and not self._content_dirty_after_upload
        if step == 1:
            return self._selected_music is not None and self._location_applied
        if step == 2:
            return self._can_review()
        return bool(self._preflight_fingerprint)

    def _busy(self) -> bool:
        return any(
            self.runner.is_running(key)
            for key in (
                "douyin_commerce_upload",
                "douyin_commerce_music",
                "douyin_commerce_location",
                "douyin_commerce_declaration",
                "douyin_commerce_preflight",
                "douyin_commerce_submit",
            )
        )

    def _content_is_valid(self) -> bool:
        return bool(
            self._selected_account()
            and self._selected_video()
            and self.title_input.text().strip()
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
            and not self._content_dirty_after_upload
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
        return [
            item.strip().lstrip("#")
            for item in self.tags_input.text().replace("，", ",").replace(" ", ",").split(",")
            if item.strip().lstrip("#")
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
            "title": self.title_input.text().strip(),
            "description": self.description_input.toPlainText().strip(),
            "tags": self._tags(),
            "fileList": [str(video.get("storedPath") or "")],
            "musicMode": "favorite-manual",
            "accountList": [str(account.get("filePath") or "")],
            "enableTimer": False,
            "runtimeMode": "preflight",
            "debugDryRun": True,
            "backgroundMode": False,
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
            "backgroundMode": False,
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
        if self.timer_enabled.isChecked():
            try:
                schedule = f"北京时间 {self._schedule_text()}"
            except ValueError as exc:
                schedule = str(exc)
        else:
            schedule = "立即发表（最终仍需单独确认）"
        return {
            "account": _normalized(account.get("profileName") or account.get("userName")) or "待选择",
            "video": str(video.get("filename") or "待选择"),
            "title": self.title_input.text().strip() or "待填写",
            "music": self._music_display(self._selected_music) if self._selected_music else "待由用户选择",
            "location": self._location_display(location) if self._location_applied and location else "待写入编辑页",
            "declaration": (
                self._selected_declaration()
                if self._declaration_applied and self._selected_declaration()
                else "待选择并回读"
            ),
            "schedule": schedule,
        }

    def start_upload(self) -> None:
        try:
            payload = self.collect_upload_payload()
        except (ValueError, douyin_commerce_service.DouyinCommerceError) as exc:
            QMessageBox.warning(self, "上传并继续", str(exc))
            return
        if self.runner.is_running("douyin_commerce_upload"):
            return
        self.upload_button.setEnabled(False)
        self.upload_button.setText("正在上传")
        self.status_badge.setText("后台上传中")

        self.runner.run(
            "douyin_commerce_upload",
            lambda: douyin_commerce_session.commerce_session_manager.start_upload(payload),
            on_success=self._upload_succeeded,
            on_error=lambda message: self._show_error("上传并继续", message),
            on_finished=self._sync_view,
        )

    def _upload_succeeded(self, result: dict) -> None:
        self._session_id = _normalized(result.get("sessionId"))
        self._music_candidates = []
        self._selected_music = None
        self.music_list.clear()
        self.music_card.setText("尚未选择收藏音乐")
        self.music_status.setText(str(result.get("message") or "视频上传完成"))
        self.location_result_list.clear()
        self.location_card.setText("尚未选择发布定位")
        self._location_applied = False
        self.location_scope_combo.blockSignals(True)
        self.location_scope_combo.setCurrentIndex(2)
        self.location_scope_combo.blockSignals(False)
        self._clear_declaration("请先选择音乐、地点范围和发布定位")
        self._preflight_fingerprint = ""
        self._content_dirty_after_upload = False
        self.content_notice.setText("视频已上传一次；后续请选择音乐、地点范围、发布定位和作品内容声明，不会重复上传。")
        self._go_to_step(1)

    def load_music(self) -> None:
        if not self._session_id:
            QMessageBox.warning(self, "读取收藏音乐", "请先上传视频。")
            return
        if self.runner.is_running("douyin_commerce_music"):
            return
        self.load_music_button.setEnabled(False)
        self.load_music_button.setText("正在读取")
        self.music_status.setText("正在从当前抖音编辑页读取收藏音乐…")
        self.runner.run(
            "douyin_commerce_music",
            lambda: douyin_commerce_session.commerce_session_manager.load_favorite_music(self._session_id),
            on_success=self._show_music_candidates,
            on_error=lambda message: self._show_error("读取收藏音乐", message),
            on_finished=lambda: self._reset_button(self.load_music_button, "读取收藏音乐"),
        )

    def _show_music_candidates(self, rows: list[dict[str, str]]) -> None:
        self._music_candidates = [dict(item) for item in rows]
        self.music_list.blockSignals(True)
        self.music_list.clear()
        for row in self._music_candidates:
            item = QListWidgetItem(
                f"{row.get('title') or ''}\n{row.get('creator') or ''} · {row.get('duration') or ''}"
            )
            item.setData(Qt.ItemDataRole.UserRole, row)
            self.music_list.addItem(item)
        self.music_list.blockSignals(False)
        self.music_status.setText(f"已读取 {len(self._music_candidates)} 首收藏音乐，请选择一首后继续。")
        self._sync_view()

    def use_selected_music(self) -> None:
        candidate = self._selected_music_candidate()
        if not candidate or not self._session_id:
            QMessageBox.warning(self, "使用所选音乐", "请先从当前收藏列表选择一首音乐。")
            return
        if self.runner.is_running("douyin_commerce_music"):
            return
        self.use_music_button.setEnabled(False)
        self.use_music_button.setText("正在使用")
        self.music_status.setText("正在将用户所选音乐写入抖音编辑页并回读…")
        music_id = str(candidate.get("musicId") or "")
        self.runner.run(
            "douyin_commerce_music",
            lambda: douyin_commerce_session.commerce_session_manager.select_favorite_music(
                self._session_id, music_id
            ),
            on_success=self._music_selected,
            on_error=lambda message: self._show_error("使用所选音乐", message),
            on_finished=lambda: self._reset_button(self.use_music_button, "使用所选音乐"),
        )

    def _music_selected(self, music: dict[str, str]) -> None:
        self._selected_music = dict(music)
        self._music_candidates = []
        self.music_list.clear()
        self.music_card.setText(self._music_display(self._selected_music))
        self.music_status.setText("音乐已由当前抖音编辑页回读确认。现在可搜索发布定位。")
        self._preflight_fingerprint = ""
        self._sync_view()

    def search_locations(self) -> None:
        keyword = self.location_keyword.text()
        scope = self._selected_location_scope()
        if not self._session_id or self._selected_music is None:
            QMessageBox.warning(self, "搜索发布定位", "请先完成视频上传和用户自选音乐。")
            return
        if not scope:
            QMessageBox.warning(self, "搜索发布定位", "请先选择地点范围：本地或国内。")
            return
        if self.runner.is_running("douyin_commerce_location"):
            return
        self.location_search_button.setEnabled(False)
        self.location_search_button.setText("正在搜索")
        self.location_status.setText(
            f"正在按“{douyin_commerce_service.location_scope_label(scope)}”范围，从当前抖音编辑页读取候选…"
        )
        self.runner.run(
            "douyin_commerce_location",
            lambda: douyin_commerce_session.commerce_session_manager.search_locations(
                self._session_id, keyword, scope
            ),
            on_success=self._show_locations,
            on_error=self._location_search_error,
            on_finished=lambda: self._reset_button(self.location_search_button, "搜索地点"),
        )

    def _show_locations(self, rows: list[dict[str, str]]) -> None:
        self._locations = [dict(row) for row in rows]
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
            self.location_card.setText("未找到可选择的发布定位，请更换关键词")
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
        self._clear_declaration("发布定位候选已更新，请选择一项并在编辑页确认")
        self._sync_view()

    def _location_search_error(self, message: str) -> None:
        self.location_status.setText(message)
        self._show_error("发布定位搜索失败", message)

    def apply_selected_location(self) -> None:
        location = self._selected_location()
        if not location or not self._session_id:
            QMessageBox.warning(self, "使用所选发布定位", "请选择一项平台发布定位候选。")
            return
        if self.runner.is_running("douyin_commerce_location"):
            return
        self.apply_location_button.setEnabled(False)
        self.apply_location_button.setText("正在确认")
        self.location_status.setText("正在选择发布定位、确认带货模式并回读…（本次不绑定门店）")
        self.location_card.setText("正在由当前抖音编辑页确认发布定位…")
        self.runner.run(
            "douyin_commerce_location",
            lambda: douyin_commerce_session.commerce_session_manager.apply_location(
                self._session_id, location
            ),
            on_success=self._location_applied_success,
            on_error=lambda message: self._show_error("使用所选发布定位", message),
            on_finished=lambda: self._reset_button(self.apply_location_button, "使用所选发布定位"),
        )

    def _location_applied_success(self, result: dict) -> None:
        location = result.get("location") if isinstance(result, dict) else None
        if not isinstance(location, dict):
            self._location_search_error("抖音未返回完整发布定位回读，已停止后续设置")
            return
        self._location_applied = True
        self.location_card.setText(self._location_display(dict(location)))
        self.location_status.setText("发布定位、带货模式和完整地址已由当前抖音编辑页回读确认；本次未绑定门店。")
        self._clear_declaration("发布定位已确认；请选择作品内容声明并写入当前编辑页。")
        self._preflight_fingerprint = ""
        self._sync_view()

    def apply_selected_declaration(self) -> None:
        declaration = self._selected_declaration()
        if not declaration or not self._session_id:
            QMessageBox.warning(self, "作品内容声明", "请先选择一项作品内容声明。")
            return
        if self.runner.is_running("douyin_commerce_declaration"):
            return
        self.apply_declaration_button.setEnabled(False)
        self.apply_declaration_button.setText("正在写入")
        self.declaration_status.setText("正在将用户所选声明写入抖音编辑页并回读…")
        self.runner.run(
            "douyin_commerce_declaration",
            lambda: douyin_commerce_session.commerce_session_manager.select_content_declaration(
                self._session_id, declaration
            ),
            on_success=self._declaration_applied_success,
            on_error=self._declaration_error,
            on_finished=lambda: self._reset_button(self.apply_declaration_button, "写入所选声明"),
        )

    def _declaration_applied_success(self, declaration: str) -> None:
        selected = _normalized(declaration)
        if selected != self._selected_declaration():
            self._show_error("作品内容声明", "抖音页面回读与用户所选声明不一致，已停止后续设置")
            return
        self._declaration_applied = True
        self.declaration_card.setText(f"{selected}\n已由当前抖音编辑页回读确认")
        self.declaration_status.setText("作品内容声明已由当前抖音编辑页回读确认。")
        self._preflight_fingerprint = ""
        self._sync_view()

    def _declaration_error(self, message: str) -> None:
        self.declaration_status.setText(message)
        self._show_error("作品内容声明", message)

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
            lambda: douyin_commerce_session.commerce_session_manager.submit(self._session_id, payload),
            on_success=lambda result: self._submit_succeeded(task, result),
            on_error=lambda message: self._submit_failed(task, message),
            on_finished=self._submit_finished,
        )
        self._sync_view()

    def _submit_succeeded(self, task: dict, result: dict) -> None:
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

    def _abandon_session(self, *, silent: bool) -> None:
        session_id = self._session_id
        self._session_id = ""
        if session_id:
            douyin_commerce_session.commerce_session_manager.close(session_id)
        self._music_candidates = []
        self._selected_music = None
        self.music_list.clear()
        self.music_card.setText("尚未选择收藏音乐")
        self.music_status.setText("本次上传会话已结束")
        self._locations = []
        self.location_result_list.clear()
        self.location_card.setText("尚未选择发布定位")
        self._location_applied = False
        self.location_scope_combo.blockSignals(True)
        self.location_scope_combo.setCurrentIndex(2)
        self.location_scope_combo.blockSignals(False)
        self._clear_declaration("请重新上传视频后选择地点与作品内容声明")
        self._preflight_fingerprint = ""
        self._content_dirty_after_upload = False
        if not silent:
            QMessageBox.information(self, "抖音带货", "已关闭临时编辑页，未保存草稿或发布。")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 固定事件名
        if self._session_id and not self._busy():
            self._abandon_session(silent=True)
        super().closeEvent(event)
