# -*- coding: utf-8 -*-
"""发布中心页面。"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QDate, QEvent, QSize, QTime, QTimer, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDateEdit,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStyle,
    QTabBar,
    QTextEdit,
    QTimeEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app_core import (
    account_browser_service,
    account_service,
    collection_service,
    content_bundle,
    douyin_location_service,
    media_service,
    mobai_release_importer,
    publish_config_service,
    publish_service,
    task_service,
    wechat_content_bundle,
    wechat_publish_policy,
    oneclick_capabilities,
)
from app_core.paths import VIDEO_DIR
from app_core.meta_browser_policy import (
    META_BROWSER_AUTOMATION_ACKNOWLEDGED,
    META_BROWSER_PUBLISH_CONFIRMED,
)
from app_core.wechat_verification import verification_broker

from .common import ROOT_DIR, button
from .background_task import BackgroundTaskRunner
from .login_dialog import LoginDialog
from .media_context_menu import build_media_context_menu
from .platform_open import open_path, reveal_in_folder
from .timer_dialog import TimerDialog
from .wechat_verification_dialog import WechatVerificationDialog


BILI_PARTITIONS = [
    "知识",
    "人工智能",
    "科技数码",
    "生活经验",
    "生活兴趣",
    "游戏",
    "动画",
    "绘画",
    "音乐",
    "影视",
    "娱乐",
    "舞蹈",
    "鬼畜",
    "时尚美妆",
    "资讯",
    "美食",
    "动物",
    "汽车",
    "体育运动",
    "健身",
    "家装房产",
    "户外潮流",
    "手工",
    "小剧场",
    "旅游出行",
    "三农",
    "亲子",
    "健康",
    "情感",
    "vlog",
]

PLATFORM_TARGET_ROLE = int(Qt.ItemDataRole.UserRole) + 1
HISTORY_DEFAULT_HEIGHT = 144
HISTORY_MIN_HEIGHT = 56
PLATFORM_TEXT_HEIGHT = 166
PLATFORM_TAGS_HEIGHT = 72
COMMON_TEXT_HEIGHT = 100
COMMON_TAGS_HEIGHT = 48
PLATFORM_COVER_PANEL_MIN_WIDTH = 190
PLATFORM_COVER_PANEL_MAX_WIDTH = 206
PLATFORM_NAV_WIDTH = 136
COMPACT_PUBLISH_TARGET_WIDTH = 255
WINDOWS_PUBLISH_TARGET_WIDTH = 340
COMPACT_PUBLISH_TARGET_MIN_WIDTH = 220
WINDOWS_PUBLISH_TARGET_MIN_WIDTH = 300
COMPACT_PUBLISH_ACTIVITY_WIDTH = 228
COMPACT_PUBLISH_ACTIVITY_MIN_WIDTH = 196
PUBLISH_CONTENT_INITIAL_WIDTH = 700

def publish_target_widths(platform_name: str) -> tuple[int, int]:
    """返回当前桌面平台的发布对象初始宽度和最小宽度。"""

    if platform_name == "win32":
        return WINDOWS_PUBLISH_TARGET_WIDTH, WINDOWS_PUBLISH_TARGET_MIN_WIDTH
    return COMPACT_PUBLISH_TARGET_WIDTH, COMPACT_PUBLISH_TARGET_MIN_WIDTH


def publish_activity_widths(platform_name: str) -> tuple[int, int]:
    """返回执行活动栏的初始宽度和最小宽度。"""

    if platform_name == "win32":
        return WINDOWS_PUBLISH_TARGET_WIDTH, WINDOWS_PUBLISH_TARGET_MIN_WIDTH
    return COMPACT_PUBLISH_ACTIVITY_WIDTH, COMPACT_PUBLISH_ACTIVITY_MIN_WIDTH


class HeaderTabStack(QStackedWidget):
    """将标签栏放进标题行，同时保留 QTabWidget 的常用接口。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._tab_bar = QTabBar()
        self._tab_bar.setObjectName("contentTabBar")
        self._tab_bar.setAccessibleName("发布内容类型切换")
        self._tab_bar.setDrawBase(False)
        self._tab_bar.setExpanding(False)
        self._tab_bar.currentChanged.connect(super().setCurrentIndex)
        self.currentChanged.connect(self._sync_tab_bar)

    def tabBar(self) -> QTabBar:
        return self._tab_bar

    def addTab(self, widget: QWidget, label: str) -> int:
        index = super().addWidget(widget)
        self._tab_bar.insertTab(index, label)
        return index

    def tabText(self, index: int) -> str:
        return self._tab_bar.tabText(index)

    def setTabToolTip(self, index: int, tooltip: str) -> None:
        self._tab_bar.setTabToolTip(index, tooltip)

    def tabToolTip(self, index: int) -> str:
        return self._tab_bar.tabToolTip(index)

    def setCurrentIndex(self, index: int) -> None:
        super().setCurrentIndex(index)
        self._tab_bar.setCurrentIndex(index)

    def _sync_tab_bar(self, index: int) -> None:
        if self._tab_bar.currentIndex() != index:
            self._tab_bar.setCurrentIndex(index)


class ClickOpenComboBox(QComboBox):
    """可编辑下拉框：点击文字区或箭头都会展开选项。"""

    def setEditable(self, editable: bool) -> None:
        previous_line_edit = self.lineEdit()
        if previous_line_edit:
            previous_line_edit.removeEventFilter(self)
        super().setEditable(editable)
        if editable and self.lineEdit():
            self.lineEdit().installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        if (
            watched is self.lineEdit()
            and event.type() == QEvent.Type.MouseButtonRelease
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self.showPopup()
            return True
        return super().eventFilter(watched, event)


class CoverPreviewCanvas(QWidget):
    """在可用区域内按目标比例绘制封面预览。"""

    def __init__(self, ratio_width: int, ratio_height: int, parent=None) -> None:
        super().__init__(parent)
        self._ratio = ratio_width / ratio_height
        self._source_pixmap = QPixmap()
        self._empty_text = "未选择"
        self.setObjectName("coverPreviewCanvas")
        self.setMinimumHeight(78)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_preview(self, pixmap: QPixmap | None, empty_text: str = "未选择") -> None:
        self._source_pixmap = pixmap or QPixmap()
        self._empty_text = empty_text
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        available = self.rect().adjusted(8, 8, -8, -8)
        if available.width() / max(1, available.height()) > self._ratio:
            frame_height = available.height()
            frame_width = round(frame_height * self._ratio)
        else:
            frame_width = available.width()
            frame_height = round(frame_width / self._ratio)
        frame = available.adjusted(
            (available.width() - frame_width) // 2,
            (available.height() - frame_height) // 2,
            -(available.width() - frame_width + 1) // 2,
            -(available.height() - frame_height + 1) // 2,
        )

        border = QPen(QColor("#B8C4D1" if not self._source_pixmap.isNull() else "#C9D2DD"))
        if self._source_pixmap.isNull():
            border.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(border)
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawRoundedRect(frame, 6, 6)

        if self._source_pixmap.isNull():
            painter.setPen(QColor("#667085"))
            painter.drawText(frame, Qt.AlignmentFlag.AlignCenter, self._empty_text)
            return
        target_size = QSize(max(1, frame.width() - 12), max(1, frame.height() - 12))
        pixmap = self._source_pixmap.scaled(
            target_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = frame.x() + (frame.width() - pixmap.width()) // 2
        y = frame.y() + (frame.height() - pixmap.height()) // 2
        painter.drawPixmap(x, y, pixmap)


class PublishConfirmDialog(QDialog):
    """发布前确认弹窗。"""

    def __init__(self, summary: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("发布前确认")
        self.resize(680, 520)
        layout = QVBoxLayout(self)
        title = QLabel("请确认本次发布配置")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)

        viewer = QTextEdit()
        viewer.setReadOnly(True)
        viewer.setPlainText(summary)
        layout.addWidget(viewer)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确认执行")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("返回修改")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class MetaBrowserPublishConfirmDialog(QDialog):
    """Meta 可见浏览器正式发布的第二道显式确认。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("确认 Meta 浏览器自动发布")
        self.resize(620, 340)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)
        title = QLabel("Instagram / Facebook 将执行最终发布")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)
        explanation = QLabel(
            "一键发会复用本机 Meta 登录状态，自动上传、填写并点击 "
            "Publish 或 Schedule。\n\n"
            "这是恢复的受控浏览器能力：必须保持窗口可见；"
            "如果 Meta 要求验证码、双重验证或安全检查，流程会停下等待用户。"
            "只有收到平台成功提示或进入内容管理页才会记为成功。"
        )
        explanation.setWordWrap(True)
        explanation.setObjectName("warningCallout")
        layout.addWidget(explanation)
        self.acknowledgement = QCheckBox(
            "我已核对全部内容，并确认使用可见浏览器执行 Meta 最终发布"
        )
        layout.addWidget(self.acknowledgement)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        confirm = buttons.button(QDialogButtonBox.StandardButton.Ok)
        confirm.setText("确认并自动发布")
        confirm.setEnabled(False)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("返回修改")
        self.acknowledgement.toggled.connect(confirm.setEnabled)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class WechatContentBundlePickerDialog(QDialog):
    """允许直接粘贴路径的内容包选择框，规避 macOS 原生选择框搜索限制。"""

    def __init__(self, parent=None, content_label: str = "内容包") -> None:
        super().__init__(parent)
        self.setWindowTitle(f"导入{content_label}")
        self.setMinimumWidth(620)
        self.manifest_path = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        title = QLabel(f"选择{content_label}文件夹")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)
        hint = QLabel("可直接粘贴内容包文件夹路径，程序会自动读取其中的 manifest.json。")
        hint.setWordWrap(True)
        hint.setProperty("role", "muted")
        layout.addWidget(hint)

        path_layout = QHBoxLayout()
        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("例如：/Users/你的用户名/Documents/我的公众号文章")
        self.path_input.returnPressed.connect(self.accept)
        path_layout.addWidget(self.path_input, 1)
        browse_button = button("选择文件夹", variant="secondary", compact=True)
        browse_button.clicked.connect(self._choose_folder)
        path_layout.addWidget(browse_button)
        layout.addLayout(path_layout)

        self.path_help = QLabel("也可以粘贴 manifest.json 的完整路径。")
        self.path_help.setProperty("role", "caption")
        layout.addWidget(self.path_help)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("导入内容包")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _choose_folder(self) -> None:
        initial = self.path_input.text().strip() or str(Path.home() / "Documents")
        folder = QFileDialog.getExistingDirectory(self, "选择内容包文件夹", initial)
        if folder:
            self.path_input.setText(folder)

    def accept(self) -> None:
        raw = self.path_input.text().strip()
        if not raw:
            self.path_help.setText("请粘贴内容包文件夹路径，或选择一个文件夹。")
            return
        candidate = Path(raw).expanduser()
        manifest = candidate / "manifest.json" if candidate.is_dir() else candidate
        if not manifest.is_file() or manifest.name != "manifest.json":
            self.path_help.setText("未找到 manifest.json；请确认输入的是内容包文件夹或该文件的完整路径。")
            return
        self.manifest_path = str(manifest.resolve())
        super().accept()


class PublishPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("pageRoot")
        self.timer_values = {"enableTimer": False}
        self.active_task_id: int | None = None
        self.active_task_is_preflight = False
        self.active_task_mode = "preflight"
        self.active_task_background_mode = True
        self.active_task_started_at: datetime | None = None
        self.seen_event_ids: set[int] = set()
        self._account_rows: list[dict] = []
        self._media_rows: list[dict] = []
        self._selected_account_ids: set[int] = set()
        self._selected_media_ids: set[int] = set()
        self._imported_article_image_specs: dict[int, dict[str, str]] = {}
        self._imported_ai_disclosure: dict[str, object] = {}
        self._imported_wechat_article_template = ""
        self._ai_declaration_explicitly_confirmed = False
        self._cover_rows_signature: tuple | None = None
        self._cover_pixmap_cache: dict[str, tuple[int, int, QPixmap]] = {}
        self._wechat_verification_dialog: WechatVerificationDialog | None = None
        self.collection_tasks = BackgroundTaskRunner(self)
        self.location_tasks = BackgroundTaskRunner(self)
        self.account_health_tasks = BackgroundTaskRunner(self)
        self._douyin_selected_location: dict[str, object] = {}
        self._douyin_location_query = ""
        self._douyin_location_search_timer = QTimer(self)
        self._douyin_location_search_timer.setSingleShot(True)
        self._douyin_location_search_timer.setInterval(450)
        self._douyin_location_search_timer.timeout.connect(
            self.search_douyin_locations
        )
        self.content_type = "video"

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        self.workflow_stack = QStackedWidget()
        root_layout.addWidget(self.workflow_stack)
        self.type_selector_page = self._build_content_type_selector()
        self.editor_page = QWidget()
        self.workflow_stack.addWidget(self.type_selector_page)
        self.workflow_stack.addWidget(self.editor_page)

        layout = QVBoxLayout(self.editor_page)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("发布中心")
        title.setObjectName("pageTitle")
        header.addWidget(title)
        self.change_content_type_btn = button("更换发布类型", variant="secondary", compact=True)
        self.change_content_type_btn.clicked.connect(self.show_content_type_selector)
        header.addWidget(self.change_content_type_btn)
        self.selection_summary = QLabel("0 个账号 · 0 个视频")
        self.selection_summary.setProperty("role", "countBadge")
        header.addWidget(self.selection_summary)
        self.account_health_label = QLabel("账号状态：未选择")
        self.account_health_label.setProperty("role", "muted")
        header.addWidget(self.account_health_label)
        header.addStretch()
        layout.addLayout(header)

        type_bar = QFrame()
        type_bar.setProperty("toolbar", True)
        type_layout = QHBoxLayout(type_bar)
        type_layout.setContentsMargins(12, 8, 12, 8)
        type_layout.setSpacing(10)
        self.content_type_combo = QComboBox()
        self.content_type_combo.addItem("视频发布", "video")
        self.content_type_combo.addItem("图文发布", "article")
        self.content_type_combo.addItem("文字发布", "text")
        self.content_type_combo.currentIndexChanged.connect(self._content_type_changed)
        self.content_type_combo.setVisible(False)
        self.content_type_buttons: list[QPushButton] = []
        for index, title_text in enumerate(("视频发布", "图文发布", "文字发布")):
            type_button = QPushButton(title_text)
            type_button.setObjectName("publishTypeButton")
            type_button.setCheckable(True)
            type_button.setMinimumWidth(104)
            type_button.clicked.connect(
                lambda _checked=False, current_index=index: self._select_content_type(
                    current_index
                )
            )
            self.content_type_buttons.append(type_button)
            type_layout.addWidget(type_button)
        self.content_type_buttons[0].setChecked(True)
        type_layout.addStretch()
        category_label = QLabel("素材分类")
        category_label.setProperty("role", "caption")
        type_layout.addWidget(category_label)
        self.media_category_combo = QComboBox()
        self.media_category_combo.setMinimumWidth(156)
        self.media_category_combo.currentIndexChanged.connect(self._media_category_changed)
        type_layout.addWidget(self.media_category_combo)
        layout.addWidget(type_bar)

        mode_bar = QFrame()
        mode_bar.setObjectName("modeBar")
        mode_bar.setProperty("toolbar", True)
        mode_layout = QHBoxLayout(mode_bar)
        mode_layout.setContentsMargins(12, 8, 12, 8)
        mode_layout.setSpacing(10)
        self.preflight = QCheckBox("预发布检查")
        self.preflight.setChecked(True)
        self.preflight.setToolTip("只完成上传与表单检查，不点击最终发布按钮。")
        self.preflight.toggled.connect(self._sync_publish_mode)
        mode_layout.addWidget(self.preflight)
        mode_layout.addSpacing(8)
        self.background_mode = QCheckBox("后台运行")
        self.background_mode.setChecked(True)
        self.background_mode.setToolTip(
            "勾选后使用无窗口浏览器，不显示页面、不抢占当前操作；"
            "关闭后显示自动化浏览器，便于人工检查。"
        )
        mode_layout.addWidget(self.background_mode)
        self.run_mode_label = QLabel("预发布检查")
        self.run_mode_label.setProperty("role", "countBadge")
        mode_layout.addWidget(self.run_mode_label)
        mode_layout.addStretch()

        for label, content_type in (("视频包", "video"), ("图文包", "article"), ("文字包", "text")):
            package_btn = button(f"导入{label}", variant="secondary")
            package_btn.setToolTip("只带入本地内容与素材，不会上传、保存草稿或发表。")
            package_btn.clicked.connect(
                lambda _checked=False, expected=content_type: self.import_content_bundle(expected)
            )
            mode_layout.addWidget(package_btn)
        self.refresh_btn = button("刷新", variant="secondary")
        self.refresh_btn.clicked.connect(lambda: self.refresh(force=True))
        mode_layout.addWidget(self.refresh_btn)
        self.save_platform_draft_btn = button("保存平台草稿", variant="secondary")
        self.save_platform_draft_btn.setToolTip(
            "仅支持视频号和B站：上传视频并填写平台数据，只保存草稿，不公开发布。"
        )
        self.save_platform_draft_btn.clicked.connect(self.create_draft_task)
        mode_layout.addWidget(self.save_platform_draft_btn)
        self.start_btn = button("开始预检", variant="primary")
        self.start_btn.clicked.connect(self.create_task)
        mode_layout.addWidget(self.start_btn)
        layout.addWidget(mode_bar)

        template_bar = QFrame()
        template_bar.setProperty("toolbar", True)
        template_layout = QHBoxLayout(template_bar)
        template_layout.setContentsMargins(12, 9, 12, 9)
        template_layout.setSpacing(8)
        template_title = QLabel("发布模板")
        template_title.setProperty("role", "caption")
        template_layout.addWidget(template_title)
        self.template_combo = QComboBox()
        self.template_combo.setMinimumWidth(200)
        template_layout.addWidget(self.template_combo)
        apply_btn = button("应用", variant="secondary", compact=True)
        apply_btn.clicked.connect(self.apply_template)
        template_layout.addWidget(apply_btn)
        save_btn = button("另存为", variant="secondary", compact=True)
        save_btn.clicked.connect(self.save_template)
        template_layout.addWidget(save_btn)
        del_btn = button("删除", variant="danger", compact=True)
        del_btn.clicked.connect(self.delete_template)
        template_layout.addWidget(del_btn)
        template_layout.addSpacing(10)
        self.save_content_btn = button("保存填写内容", variant="primary", compact=True)
        self.save_content_btn.setToolTip("只在本机保存账号、视频、封面和全部填写内容")
        self.save_content_btn.clicked.connect(self.save_publish_content)
        template_layout.addWidget(self.save_content_btn)
        self.restore_content_btn = button("恢复内容", variant="secondary", compact=True)
        self.restore_content_btn.setToolTip("恢复上次保存的完整发布内容")
        self.restore_content_btn.clicked.connect(
            lambda: self.restore_publish_content()
        )
        template_layout.addWidget(self.restore_content_btn)
        self.content_save_status = QLabel("尚未保存")
        self.content_save_status.setProperty("role", "muted")
        template_layout.addWidget(self.content_save_status)
        template_layout.addStretch()
        layout.addWidget(template_bar)

        progress_bar = QFrame()
        progress_bar.setObjectName("taskProgressBar")
        progress_bar.setProperty("subPanel", True)
        progress_layout = QHBoxLayout(progress_bar)
        progress_layout.setContentsMargins(12, 8, 12, 8)
        progress_layout.setSpacing(12)
        self.task_status_label = QLabel("发布任务：空闲")
        self.task_status_label.setProperty("role", "muted")
        self.task_status_label.setMinimumWidth(250)
        progress_layout.addWidget(self.task_status_label)
        self.task_progress = QProgressBar()
        self.task_progress.setRange(0, 1)
        self.task_progress.setValue(0)
        self.task_progress.setFormat("空闲")
        self.task_progress.setFixedHeight(20)
        progress_layout.addWidget(self.task_progress, 1)
        layout.addWidget(progress_bar)

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setChildrenCollapsible(False)
        layout.addWidget(self.main_splitter, 1)
        self.main_splitter.addWidget(self._left_panel())
        self.main_splitter.addWidget(self._middle_panel())
        self.main_splitter.addWidget(self._right_panel())
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setStretchFactor(2, 0)
        target_width, _target_min_width = publish_target_widths(sys.platform)
        activity_width, _activity_min_width = publish_activity_widths(sys.platform)
        self.main_splitter.setSizes(
            [target_width, PUBLISH_CONTENT_INITIAL_WIDTH, activity_width]
        )

        self.task_timer = QTimer(self)
        self.task_timer.setInterval(1200)
        self.task_timer.timeout.connect(self.poll_task)
        self.refresh()
        self.restore_publish_content(show_message=False)
        self._sync_content_type_interface()
        self.workflow_stack.setCurrentWidget(self.type_selector_page)

    def _mark_ai_declaration_explicitly_confirmed(self, checked: bool) -> None:
        """只把用户亲自点击视为平台 AI 声明授权。"""

        self._ai_declaration_explicitly_confirmed = bool(checked)

    def _build_content_type_selector(self) -> QWidget:
        """发布中心入口：先明确内容类型，再进入对应的适配界面。"""

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(52, 40, 52, 48)
        layout.setSpacing(16)
        layout.addStretch(1)

        eyebrow = QLabel("发布中心 · 第一步")
        eyebrow.setProperty("role", "caption")
        eyebrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(eyebrow)
        title = QLabel("这次要发布什么内容？")
        title.setObjectName("pageTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        description = QLabel(
            "先选择内容类型，一键发会展示相应的素材、发布信息与可选账号。"
        )
        description.setProperty("role", "muted")
        description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(description)
        layout.addSpacing(14)

        # 入口只承担“选择发布类型”这一件事；素材和字段说明进入下一页再展示。
        # 限制宽度避免大屏上变成长条，保持三个入口的视觉重心。
        cards_widget = QWidget()
        cards_widget.setObjectName("publishTypeEntryGroup")
        cards_widget.setFixedWidth(960)
        cards = QHBoxLayout(cards_widget)
        cards.setContentsMargins(0, 0, 0, 0)
        cards.setSpacing(16)
        self.entry_type_buttons: list[QPushButton] = []
        for index, title_text in enumerate(("视频发布", "图文发布", "文字发布")):
            card = QPushButton(title_text)
            card.setObjectName("publishTypeEntryButton")
            card.setMinimumSize(240, 156)
            card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            card.setCursor(Qt.CursorShape.PointingHandCursor)
            card.clicked.connect(
                lambda _checked=False, current_index=index: self._enter_content_type(
                    current_index
                )
            )
            self.entry_type_buttons.append(card)
            cards.addWidget(card, 1)
        layout.addWidget(cards_widget, 0, Qt.AlignmentFlag.AlignHCenter)

        hint = QLabel("选择后进入对应发布流程，可随时返回切换类型。")
        hint.setProperty("role", "muted")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)
        layout.addStretch(2)
        return page

    def show_content_type_selector(self) -> None:
        self.workflow_stack.setCurrentWidget(self.type_selector_page)

    def _enter_content_type(self, index: int) -> None:
        self._select_content_type(index)
        self.workflow_stack.setCurrentWidget(self.editor_page)

    def _available_platform_types(self) -> set[int]:
        """按一键发本地能力矩阵过滤当前内容类型的账号目标。"""

        return {
            platform_type
            for platform_type, name in account_service.PLATFORMS.items()
            if oneclick_capabilities.supports(name, self.content_type)
        }

    def _left_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("publishTargetPanel")
        panel.setProperty("workspace", True)
        _target_width, target_min_width = publish_target_widths(sys.platform)
        panel.setMinimumWidth(target_min_width)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        title_row = QHBoxLayout()
        title = QLabel("发布对象")
        title.setProperty("role", "sectionTitle")
        title_row.addWidget(title)
        title_row.addStretch()
        layout.addLayout(title_row)

        filter_grid = QGridLayout()
        filter_grid.setHorizontalSpacing(8)
        filter_grid.setVerticalSpacing(4)
        self.profile_filter = QComboBox()
        self.profile_filter.currentIndexChanged.connect(self.refresh_accounts)
        self.platform_filter = QComboBox()
        self.platform_filter.currentIndexChanged.connect(self.refresh_accounts)
        profile_label = QLabel("账号主体")
        profile_label.setProperty("role", "caption")
        platform_label = QLabel("平台")
        platform_label.setProperty("role", "caption")
        filter_grid.addWidget(profile_label, 0, 0)
        filter_grid.addWidget(platform_label, 0, 1)
        filter_grid.addWidget(self.profile_filter, 1, 0)
        filter_grid.addWidget(self.platform_filter, 1, 1)
        layout.addLayout(filter_grid)

        account_header = QHBoxLayout()
        self.selected_account_label = QLabel("已选择账号：0")
        self.selected_account_label.setProperty("role", "caption")
        account_header.addWidget(self.selected_account_label)
        account_header.addStretch()
        select_accounts = button("全选", variant="ghost", compact=True)
        select_accounts.clicked.connect(lambda: self._set_list_checked(self.account_list, True))
        account_header.addWidget(select_accounts)
        self.deselect_all_accounts_btn = button("取消全选", variant="ghost", compact=True)
        self.deselect_all_accounts_btn.clicked.connect(
            lambda: self._set_list_checked(self.account_list, False)
        )
        account_header.addWidget(self.deselect_all_accounts_btn)
        layout.addLayout(account_header)

        self.account_list = QListWidget()
        self.account_list.setObjectName("accountTargetList")
        self.account_list.setWordWrap(False)
        self.account_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.account_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.account_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.account_list.customContextMenuRequested.connect(self.open_account_menu)
        self.account_list.itemChanged.connect(self._account_item_changed)
        layout.addWidget(self.account_list, 1)
        return panel

    def _middle_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("publishContentPanel")
        panel.setProperty("workspace", True)
        panel.setMinimumWidth(450)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        content_header = QHBoxLayout()
        content_header.setContentsMargins(0, 0, 0, 0)
        title = QLabel("发布内容")
        title.setProperty("role", "sectionTitle")
        content_header.addWidget(title)
        content_header.addSpacing(18)
        self.content_tabs = HeaderTabStack()
        self.content_tabs.setObjectName("contentStack")
        self.content_tabs.tabBar().setCursor(Qt.CursorShape.PointingHandCursor)
        self.content_tabs.tabBar().setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        content_header.addWidget(self.content_tabs.tabBar())
        content_header.addStretch()
        self.clear_content_btn = button(
            "清空已编辑内容",
            variant="danger",
            compact=True,
        )
        self.clear_content_btn.setToolTip(
            "清空通用内容和全部平台适配项，不影响已选账号、视频素材和执行日志"
        )
        self.clear_content_btn.clicked.connect(self.clear_edited_content)
        content_header.addWidget(self.clear_content_btn)
        layout.addLayout(content_header)
        layout.addWidget(self.content_tabs, 1)

        common_page = QWidget()
        common_layout = QVBoxLayout(common_page)
        common_layout.setContentsMargins(12, 10, 12, 10)
        common_layout.setSpacing(8)

        common_columns = QHBoxLayout()
        common_columns.setContentsMargins(0, 0, 0, 0)
        common_columns.setSpacing(14)
        self.common_left_column = QWidget()
        self.common_left_column.setObjectName("commonLeftColumn")
        left_layout = QVBoxLayout(self.common_left_column)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)
        self.common_right_column = QWidget()
        self.common_right_column.setObjectName("commonRightColumn")
        right_layout = QVBoxLayout(self.common_right_column)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)
        common_columns.addWidget(self.common_left_column, 3)
        common_columns.addWidget(self.common_right_column, 2)
        common_layout.addLayout(common_columns, 1)

        content_title = QLabel("文案与话题")
        content_title.setProperty("role", "sectionTitle")
        left_layout.addWidget(content_title)
        content_form = QFormLayout()
        content_form.setContentsMargins(0, 0, 0, 0)
        content_form.setHorizontalSpacing(14)
        content_form.setVerticalSpacing(7)
        content_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        content_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.common_title_input = QLineEdit()
        self.common_title_input.setPlaceholderText("各平台标题留空时使用这里的标题")
        content_form.addRow("通用标题", self.common_title_input)
        self.title_input = QTextEdit()
        self.title_input.setPlaceholderText("各平台文案留空时使用这里的正文")
        self.title_input.setFixedHeight(COMMON_TEXT_HEIGHT)
        content_form.addRow("通用文案", self.title_input)
        self.tags_input = QTextEdit()
        self.tags_input.setPlaceholderText("#AI编程 #程序员 #效率工具")
        self.tags_input.setAcceptRichText(False)
        self.tags_input.setTabChangesFocus(True)
        self.tags_input.setFixedHeight(COMMON_TAGS_HEIGHT)
        content_form.addRow("通用话题", self.tags_input)
        left_layout.addLayout(content_form)

        history_header = QHBoxLayout()
        history_header.setContentsMargins(0, 0, 0, 0)
        history_header.setSpacing(8)
        history_title = QLabel("历史话题")
        history_title.setProperty("role", "sectionTitle")
        history_header.addWidget(history_title)
        history_header.addStretch()
        history_hint = QLabel("点击添加 · × 删除")
        history_hint.setProperty("role", "caption")
        history_header.addWidget(history_hint)
        left_layout.addLayout(history_header)
        self.tag_history = QListWidget()
        self.tag_history.setObjectName("historyTagList")
        self.tag_history.setFlow(QListWidget.Flow.LeftToRight)
        self.tag_history.setWrapping(True)
        self.tag_history.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.tag_history.setMovement(QListWidget.Movement.Static)
        self.tag_history.setSpacing(2)
        self.tag_history.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tag_history.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.tag_history.verticalScrollBar().setSingleStep(34)
        self.tag_history.setMinimumHeight(HISTORY_MIN_HEIGHT)
        self.tag_history.setMaximumHeight(HISTORY_DEFAULT_HEIGHT)
        self.tag_history.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        left_layout.addWidget(self.tag_history, 1)

        cover_header = QHBoxLayout()
        self.cover_section_title = QLabel("通用封面")
        self.cover_section_title.setObjectName("commonCoverSectionTitle")
        self.cover_section_title.setProperty("role", "sectionTitle")
        cover_header.addWidget(self.cover_section_title)
        cover_header.addStretch()
        self.cover_summary_label = QLabel("未指定")
        self.cover_summary_label.setProperty("role", "muted")
        self.cover_summary_label.setWordWrap(False)
        cover_header.addWidget(self.cover_summary_label)
        right_layout.addLayout(cover_header)
        self.cover_common = QComboBox()
        self.cover_43 = QComboBox()
        self.cover_34 = QComboBox()
        # 兼容旧代码和旧测试中的属性名；新的横版规格统一为 4:3。
        self.cover_169 = self.cover_43
        self.cover_34.setProperty("coverOrientation", "portrait")
        self.cover_43.setProperty("coverOrientation", "landscape")
        self.cover_previews: dict[QComboBox, CoverPreviewCanvas] = {}
        self.common_cover_panel = QFrame()
        self.common_cover_panel.setObjectName("commonCoverPanel")
        self.common_cover_panel.setProperty("subPanel", True)
        cover_grid = QGridLayout(self.common_cover_panel)
        cover_grid.setContentsMargins(8, 8, 8, 8)
        cover_grid.setHorizontalSpacing(10)
        cover_grid.setVerticalSpacing(6)
        for column, (label, ratio, combo, ratio_size) in enumerate(
            (
                ("竖版封面", "3:4", self.cover_34, (3, 4)),
                ("横版封面", "4:3", self.cover_43, (4, 3)),
            )
        ):
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(7)
            combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            combo.currentTextChanged.connect(combo.setToolTip)
            label_widget = QLabel(f"{label}  ·  {ratio}")
            label_widget.setProperty("role", "caption")
            preview = CoverPreviewCanvas(*ratio_size)
            combo.currentIndexChanged.connect(lambda _, c=combo: self.update_cover_preview(c))
            combo.currentIndexChanged.connect(self.update_cover_summary)
            self.cover_previews[combo] = preview
            cover_grid.addWidget(label_widget, 0, column)
            cover_grid.addWidget(preview, 1, column)
            cover_grid.addWidget(combo, 2, column)
            cover_grid.setColumnStretch(column, 1)
        cover_grid.setRowStretch(1, 1)
        right_layout.addWidget(self.common_cover_panel, 1)

        rules_title = QLabel("发布规则")
        rules_title.setObjectName("publishRulesSectionTitle")
        rules_title.setProperty("role", "sectionTitle")
        right_layout.addWidget(rules_title)
        declaration_panel = QFrame()
        declaration_panel.setProperty("subPanel", True)
        declaration_layout = QGridLayout(declaration_panel)
        declaration_layout.setContentsMargins(10, 8, 10, 8)
        declaration_layout.setHorizontalSpacing(12)
        declaration_layout.setVerticalSpacing(6)
        self.original_declaration = QCheckBox("原创声明")
        self.original_declaration.setToolTip("仅在确认拥有完整原创权利时开启。")
        self.ai_generated_content = QCheckBox("AI 生成内容")
        self.ai_generated_content.setToolTip("作品包含 AI 生成或合成的画面、声音等内容时开启。")
        self.ai_generated_content.toggled.connect(
            self._mark_ai_declaration_explicitly_confirmed
        )
        self.common_visibility = QComboBox()
        self.common_visibility.setMinimumWidth(118)
        self.common_visibility.addItem("公开", "public")
        self.common_visibility.addItem("私密", "private")
        declaration_layout.addWidget(self.original_declaration, 0, 0)
        declaration_layout.addWidget(self.ai_generated_content, 0, 1)

        timer_row = QWidget()
        timer_layout = QHBoxLayout(timer_row)
        timer_layout.setContentsMargins(0, 0, 0, 0)
        timer_layout.setSpacing(8)
        self.common_schedule_enabled = QCheckBox("定时发布")
        self.common_schedule_date = QDateEdit()
        self.common_schedule_date.setCalendarPopup(True)
        self.common_schedule_date.setDisplayFormat("yyyy-MM-dd")
        self.common_schedule_date.setMinimumDate(QDate.currentDate())
        self.common_schedule_date.setDate(QDate.currentDate().addDays(1))
        self.common_schedule_date.setFixedWidth(100)
        self.common_schedule_time = QTimeEdit()
        self.common_schedule_time.setDisplayFormat("HH:mm")
        self.common_schedule_time.setTime(QTime(18, 0))
        self.common_schedule_time.setFixedWidth(72)
        self.common_schedule_date.setEnabled(False)
        self.common_schedule_time.setEnabled(False)
        self.common_schedule_enabled.toggled.connect(
            self._common_schedule_toggled
        )
        self.common_schedule_date.dateChanged.connect(
            self._sync_common_schedule_values
        )
        self.common_schedule_time.timeChanged.connect(
            self._sync_common_schedule_values
        )
        timer_layout.addWidget(self.common_schedule_enabled)
        timer_layout.addSpacing(8)
        timer_layout.addWidget(self.common_schedule_date)
        timer_layout.addWidget(self.common_schedule_time)
        timer_layout.addStretch()
        declaration_layout.addWidget(timer_row, 1, 0, 1, 2)

        visibility_row = QWidget()
        visibility_layout = QHBoxLayout(visibility_row)
        visibility_layout.setContentsMargins(0, 0, 0, 0)
        visibility_layout.setSpacing(8)
        visibility_layout.addWidget(QLabel("谁可以看"))
        visibility_layout.addWidget(self.common_visibility, 1)
        declaration_layout.addWidget(visibility_row, 2, 0, 1, 2)
        declaration_layout.setColumnStretch(0, 1)
        declaration_layout.setColumnStretch(1, 1)
        right_layout.addWidget(declaration_panel)
        common_scroll = QScrollArea()
        self.common_scroll = common_scroll
        common_scroll.setWidgetResizable(True)
        common_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        common_scroll.setWidget(common_page)
        self.content_tabs.addTab(common_scroll, "通用内容")
        self.content_tabs.setTabToolTip(0, "设置所有平台默认使用的标题、文案和发布选项")

        platform_page = QWidget()
        platform_layout = QHBoxLayout(platform_page)
        platform_layout.setContentsMargins(10, 10, 10, 10)
        platform_layout.setSpacing(10)
        self.platform_nav = QListWidget()
        self.platform_nav.setObjectName("platformNav")
        self.platform_nav.setFixedWidth(PLATFORM_NAV_WIDTH)
        self.platform_nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.platform_nav.currentItemChanged.connect(self._platform_nav_changed)
        platform_layout.addWidget(self.platform_nav)
        self.platform_editor_stack = QStackedWidget()
        platform_layout.addWidget(self.platform_editor_stack, 1)
        self.platform_empty_page = QLabel("请先在左侧勾选发布账号")
        self.platform_empty_page.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.platform_empty_page.setProperty("role", "muted")
        self.platform_editor_stack.addWidget(self.platform_empty_page)
        self.platform_titles: dict[int, QLineEdit] = {}
        self.platform_texts: dict[int, QTextEdit] = {}
        self.platform_tags: dict[int, QTextEdit] = {}
        self.platform_editors: dict[int, QWidget] = {}
        self.platform_categories: dict[int, QComboBox] = {}
        self.platform_visibility: dict[int, QComboBox] = {}
        self.platform_collections: dict[int, QComboBox] = {}
        self.platform_collection_buttons: dict[int, QWidget] = {}
        self.platform_collection_rows: dict[int, QWidget] = {}
        self.platform_collection_status: dict[int, QLabel] = {}
        self.platform_schedule_enabled: dict[int, QCheckBox] = {}
        self.platform_schedule_dates: dict[int, QDateEdit] = {}
        self.platform_schedule_times: dict[int, QTimeEdit] = {}
        self.platform_editor_bodies: dict[int, QWidget] = {}
        self.platform_editor_status: dict[int, QLabel] = {}
        self.platform_cover_34: dict[int, QComboBox] = {}
        self.platform_cover_43: dict[int, QComboBox] = {}
        self.platform_cover_previews: dict[tuple[int, str], CoverPreviewCanvas] = {}
        self.youtube_made_for_kids: QCheckBox | None = None
        self.youtube_notify_subscribers: QCheckBox | None = None
        self.instagram_share_to_feed: QCheckBox | None = None
        self.douyin_sync_toutiao: QCheckBox | None = None
        self.douyin_location_keyword: QLineEdit | None = None
        self.douyin_location_search_button: QPushButton | None = None
        self.douyin_location_results_title: QLabel | None = None
        self.douyin_location_results: QListWidget | None = None
        self.douyin_location_status: QLabel | None = None
        self.wechat_group_notification: QCheckBox | None = None
        for platform_type in account_service.PLATFORM_ORDER:
            editor = self._build_platform_editor(platform_type)
            self.platform_editors[platform_type] = editor
            self.platform_editor_stack.addWidget(editor)
        self.content_tabs.addTab(platform_page, "平台适配")
        self.content_tabs.setTabToolTip(1, "为不同平台覆盖通用内容和发布选项")
        return panel

    def _build_platform_editor(self, platform_type: int) -> QWidget:
        name = account_service.PLATFORMS[platform_type]
        editor = QWidget()
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(10, 10, 10, 10)
        editor_layout.setSpacing(8)

        header = QHBoxLayout()
        editor_title = QLabel(name)
        editor_title.setProperty("role", "sectionTitle")
        header.addWidget(editor_title)
        header.addStretch()
        editor_status = QLabel("未选择账号")
        editor_status.setProperty("role", "muted")
        header.addWidget(editor_status)
        editor_layout.addLayout(header)
        self.platform_editor_status[platform_type] = editor_status

        editor_body = QWidget()
        editor_body_layout = QHBoxLayout(editor_body)
        editor_body_layout.setContentsMargins(0, 4, 0, 0)
        editor_body_layout.setSpacing(12)
        settings_column = QWidget()
        body_layout = QVBoxLayout(settings_column)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(9)
        self.platform_editor_bodies[platform_type] = editor_body
        editor_body_layout.addWidget(settings_column, 1)

        content_form = QFormLayout()
        content_form.setContentsMargins(0, 0, 0, 0)
        content_form.setHorizontalSpacing(12)
        content_form.setVerticalSpacing(9)
        content_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        title = QLineEdit()
        title.setPlaceholderText("留空时使用通用标题")
        content_form.addRow("平台标题", title)
        text = QTextEdit()
        text.setPlaceholderText("留空时使用通用文案")
        text.setFixedHeight(PLATFORM_TEXT_HEIGHT)
        content_form.addRow("平台文案", text)
        tags = QTextEdit()
        tags.setPlaceholderText(f"{name} 专属话题，留空使用通用话题")
        tags.setAcceptRichText(False)
        tags.setTabChangesFocus(True)
        tags.setFixedHeight(PLATFORM_TAGS_HEIGHT)
        content_form.addRow("平台话题", tags)
        body_layout.addLayout(content_form)

        self.platform_titles[platform_type] = title
        self.platform_texts[platform_type] = text
        self.platform_tags[platform_type] = tags

        publish_settings = QFrame()
        publish_settings.setProperty("subPanel", True)
        publish_settings_layout = QFormLayout(publish_settings)
        publish_settings_layout.setContentsMargins(12, 10, 12, 10)
        publish_settings_layout.setHorizontalSpacing(14)

        collection = ClickOpenComboBox()
        collection.setObjectName("collectionCombo")
        collection.setEditable(True)
        collection.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        collection.addItem("不选择合集", "")
        collection.lineEdit().setPlaceholderText("输入或选择合集名称")
        collection.setToolTip("输入该账号已有的合集或播放列表名称；留空则不设置。")
        self.platform_collections[platform_type] = collection
        collection_sync = button("同步合集", variant="secondary", compact=True)
        collection_sync.setToolTip("使用当前勾选账号的登录状态读取平台合集")
        collection_sync.clicked.connect(
            lambda _checked=False, current_type=platform_type: self.sync_platform_collections(current_type)
        )
        self.platform_collection_buttons[platform_type] = collection_sync
        collection_status = QLabel("尚未同步")
        collection_status.setProperty("role", "muted")
        collection_status.setWordWrap(False)
        self.platform_collection_status[platform_type] = collection_status
        collection_row = QWidget()
        collection_row_layout = QHBoxLayout(collection_row)
        collection_row_layout.setContentsMargins(0, 0, 0, 0)
        collection_row_layout.setSpacing(8)
        collection_row_layout.addWidget(collection, 1)
        collection_row_layout.addWidget(collection_sync)
        collection_row_layout.addWidget(collection_status)
        self.platform_collection_rows[platform_type] = collection_row
        publish_settings_layout.addRow("合集/播放列表", collection_row)

        schedule_enabled = QCheckBox("单独设置")
        schedule_date = QDateEdit()
        schedule_date.setCalendarPopup(True)
        schedule_date.setDisplayFormat("yyyy-MM-dd")
        schedule_date.setMinimumDate(QDate.currentDate())
        schedule_date.setDate(QDate.currentDate().addDays(1))
        schedule_time = QTimeEdit()
        schedule_time.setDisplayFormat("HH:mm")
        schedule_time.setTime(QTime(18, 0))
        schedule_time.setFixedWidth(86)
        schedule_date.setEnabled(False)
        schedule_time.setEnabled(False)
        schedule_enabled.toggled.connect(schedule_date.setEnabled)
        schedule_enabled.toggled.connect(schedule_time.setEnabled)
        self.platform_schedule_enabled[platform_type] = schedule_enabled
        self.platform_schedule_dates[platform_type] = schedule_date
        self.platform_schedule_times[platform_type] = schedule_time

        schedule_row = QWidget()
        schedule_row_layout = QHBoxLayout(schedule_row)
        schedule_row_layout.setContentsMargins(0, 0, 0, 0)
        schedule_row_layout.setSpacing(8)
        schedule_row_layout.addWidget(schedule_enabled)
        schedule_row_layout.addSpacing(8)
        schedule_row_layout.addWidget(schedule_date, 1)
        schedule_row_layout.addWidget(schedule_time)
        publish_settings_layout.addRow("发布时间", schedule_row)
        body_layout.addWidget(publish_settings)

        if platform_type == 3:
            settings = QFrame()
            settings.setProperty("subPanel", True)
            settings_layout = QFormLayout(settings)
            settings_layout.setContentsMargins(12, 10, 12, 10)
            self.douyin_sync_toutiao = QCheckBox("同步发布到今日头条")
            self.douyin_sync_toutiao.setChecked(False)
            self.douyin_location_keyword = QLineEdit()
            self.douyin_location_keyword.setPlaceholderText("输入地点名称；留空不添加")
            self.douyin_location_keyword.setClearButtonEnabled(True)
            self.douyin_location_keyword.setMaxLength(80)
            self.douyin_location_keyword.setToolTip(
                "输入至少 2 个字后，使用当前抖音会话在后台搜索官方地点。"
            )
            self.douyin_location_keyword.textEdited.connect(
                self._douyin_location_text_edited
            )
            self.douyin_location_keyword.returnPressed.connect(
                self.search_douyin_locations
            )
            self.douyin_location_search_button = button(
                "搜索", variant="secondary", compact=True
            )
            self.douyin_location_search_button.setToolTip(
                "使用当前已选抖音账号的本地会话读取官方地点候选"
            )
            self.douyin_location_search_button.clicked.connect(
                self.search_douyin_locations
            )
            settings_layout.setHorizontalSpacing(14)
            settings_layout.setVerticalSpacing(10)
            settings_layout.addRow("同步设置", self.douyin_sync_toutiao)

            douyin_location_search_row = QWidget()
            douyin_location_search_layout = QHBoxLayout(douyin_location_search_row)
            douyin_location_search_layout.setContentsMargins(0, 0, 0, 0)
            douyin_location_search_layout.setSpacing(8)
            douyin_location_search_layout.addWidget(
                self.douyin_location_keyword,
                1,
            )
            douyin_location_search_layout.addWidget(
                self.douyin_location_search_button
            )
            settings_layout.addRow("发布定位", douyin_location_search_row)

            self.douyin_location_results_title = QLabel(
                "地点候选 · 名称与完整地址"
            )
            self.douyin_location_results_title.setObjectName(
                "douyinLocationResultsTitle"
            )
            self.douyin_location_results_title.setVisible(False)
            settings_layout.addRow(self.douyin_location_results_title)
            self.douyin_location_results = QListWidget()
            self.douyin_location_results.setObjectName("douyinLocationResults")
            self.douyin_location_results.setAlternatingRowColors(False)
            self.douyin_location_results.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            )
            self.douyin_location_results.setVerticalScrollMode(
                QListWidget.ScrollMode.ScrollPerPixel
            )
            self.douyin_location_results.setFixedHeight(292)
            self.douyin_location_results.itemClicked.connect(
                self._select_douyin_location_item
            )
            self.douyin_location_results.setVisible(False)
            settings_layout.addRow(self.douyin_location_results)

            self.douyin_location_status = QLabel(
                "输入至少 2 个字，一键发会在后台读取抖音官方地点"
            )
            self.douyin_location_status.setProperty("role", "muted")
            self.douyin_location_status.setWordWrap(True)
            settings_layout.addRow("", self.douyin_location_status)
            body_layout.addWidget(settings)
        elif platform_type == 5:
            settings = QFrame()
            settings.setProperty("subPanel", True)
            settings_layout = QFormLayout(settings)
            settings_layout.setContentsMargins(12, 10, 12, 10)
            self.bili_partition = QComboBox()
            self.bili_partition.addItems(BILI_PARTITIONS)
            self.bili_type = QComboBox()
            self.bili_type.addItems(["自制", "转载"])
            self.bili_settings_row = QWidget()
            bili_settings_layout = QHBoxLayout(self.bili_settings_row)
            bili_settings_layout.setContentsMargins(0, 0, 0, 0)
            bili_settings_layout.setSpacing(8)
            bili_settings_layout.addWidget(QLabel("分区"))
            bili_settings_layout.addWidget(self.bili_partition, 1)
            bili_settings_layout.addWidget(QLabel("投稿类型"))
            bili_settings_layout.addWidget(self.bili_type, 1)
            settings_layout.addRow("B站设置", self.bili_settings_row)
            body_layout.addWidget(settings)
            self.bili_title = title
            self.bili_desc = text
        elif platform_type == 7:
            settings = QFrame()
            settings.setProperty("subPanel", True)
            settings_layout = QFormLayout(settings)
            settings_layout.setContentsMargins(12, 10, 12, 10)
            visibility = QComboBox()
            visibility.addItem("使用通用设置", "")
            visibility.addItem("公开", "public")
            visibility.addItem("私密", "private")
            visibility.addItem("不公开", "unlisted")
            self.platform_visibility[platform_type] = visibility
            settings_layout.addRow("可见性", visibility)
            self.youtube_made_for_kids = QCheckBox("内容面向儿童")
            self.youtube_made_for_kids.setToolTip(
                "仅在内容确实面向儿童时开启，会写入 YouTube 官方受众字段。"
            )
            settings_layout.addRow("受众", self.youtube_made_for_kids)
            self.youtube_notify_subscribers = QCheckBox("通知订阅者")
            self.youtube_notify_subscribers.setChecked(True)
            self.youtube_notify_subscribers.setToolTip(
                "YouTube 默认会通知订阅者；取消勾选时明确传递 notifySubscribers=false。"
            )
            settings_layout.addRow("通知", self.youtube_notify_subscribers)
            body_layout.addWidget(settings)
        elif platform_type == 8:
            settings = QFrame()
            settings.setProperty("subPanel", True)
            settings_layout = QFormLayout(settings)
            settings_layout.setContentsMargins(12, 10, 12, 10)
            self.instagram_share_to_feed = QCheckBox("同时分享到 Instagram 动态")
            self.instagram_share_to_feed.setChecked(True)
            self.instagram_share_to_feed.setToolTip(
                "开启时 Reel 也会显示在主页动态；取消勾选时仅发布到 Reels。"
            )
            settings_layout.addRow("展示位置", self.instagram_share_to_feed)
            body_layout.addWidget(settings)
        elif platform_type == 10:
            settings = QFrame()
            settings.setProperty("subPanel", True)
            settings_layout = QFormLayout(settings)
            settings_layout.setContentsMargins(12, 10, 12, 10)
            self.wechat_group_notification = QCheckBox("开启群发通知")
            self.wechat_group_notification.setChecked(True)
            self.wechat_group_notification.setToolTip(
                "公众号新发布默认开启；只有你明确取消勾选时才关闭。"
                "分组通知由公众号根据群发设置联动。"
            )
            settings_layout.addRow("通知方式", self.wechat_group_notification)
            body_layout.addWidget(settings)

        body_layout.addStretch()
        editor_body_layout.addWidget(
            self._build_platform_cover_panel(platform_type),
            0,
        )
        editor_layout.addWidget(editor_body, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(editor)
        return scroll

    def _build_platform_cover_panel(self, platform_type: int) -> QWidget:
        panel = QFrame()
        panel.setObjectName(f"platformCoverPanel{platform_type}")
        panel.setProperty("subPanel", True)
        panel.setMinimumWidth(PLATFORM_COVER_PANEL_MIN_WIDTH)
        panel.setMaximumWidth(PLATFORM_COVER_PANEL_MAX_WIDTH)
        panel.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Expanding,
        )
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 10, 8, 10)
        layout.setSpacing(6)

        title = QLabel("单独封面")
        title.setProperty("role", "sectionTitle")
        layout.addWidget(title)
        hint = QLabel("未设置时使用通用封面")
        hint.setProperty("role", "muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        for label, ratio, ratio_size, target in (
            ("竖版封面", "3:4", (3, 4), self.platform_cover_34),
            ("横版封面", "4:3", (4, 3), self.platform_cover_43),
        ):
            label_widget = QLabel(f"{label}  ·  {ratio}")
            label_widget.setProperty("role", "caption")
            layout.addWidget(label_widget)
            preview = CoverPreviewCanvas(*ratio_size)
            preview.setMinimumHeight(84)
            layout.addWidget(preview, 1)
            combo = QComboBox()
            combo.setProperty(
                "coverOrientation",
                "portrait" if ratio == "3:4" else "landscape",
            )
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            combo.setMinimumContentsLength(5)
            combo.setSizePolicy(
                QSizePolicy.Policy.Ignored,
                QSizePolicy.Policy.Fixed,
            )
            combo.addItem("使用通用封面", "")
            combo.currentTextChanged.connect(combo.setToolTip)
            combo.currentIndexChanged.connect(
                lambda _index, current_type=platform_type, current_ratio=ratio:
                self._update_platform_cover_preview(current_type, current_ratio)
            )
            target[platform_type] = combo
            self.platform_cover_previews[(platform_type, ratio)] = preview
            layout.addWidget(combo)
        return panel

    def _platform_nav_changed(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if current is None:
            self.platform_editor_stack.setCurrentWidget(self.platform_empty_page)
            return
        platform_type = int(current.data(Qt.ItemDataRole.UserRole) or 0)
        editor = self.platform_editors.get(platform_type)
        if editor:
            self.platform_editor_stack.setCurrentWidget(editor)
            enabled = bool(current.data(PLATFORM_TARGET_ROLE))
            self._set_platform_editor_target_state(platform_type, enabled)
            if enabled:
                self._load_cached_collections(platform_type)
            else:
                self._set_collection_status(platform_type, "未选择该平台账号")

    def _set_platform_editor_target_state(self, platform_type: int, enabled: bool) -> None:
        accounts = self._platform_accounts(platform_type)
        body = self.platform_editor_bodies[platform_type]
        status = self.platform_editor_status[platform_type]
        body.setEnabled(enabled)
        status.setText("可编辑" if enabled else "只读")
        status.setToolTip(
            f"本次发布已选择 {len(accounts)} 个账号"
            if enabled
            else "该平台未选择发布账号"
        )
        status.setProperty("role", "countBadge" if enabled else "muted")
        status.style().unpolish(status)
        status.style().polish(status)

    def _platform_accounts(self, platform_type: int) -> list[dict]:
        return [
            account
            for account in self.selected_accounts()
            if int(account.get("type") or 0) == int(platform_type)
        ]

    def _set_collection_status(self, platform_type: int, text: str, role: str = "muted") -> None:
        label = self.platform_collection_status[platform_type]
        label.setText(text)
        label.setProperty("role", role)
        label.style().unpolish(label)
        label.style().polish(label)

    def _set_collection_options(
        self,
        platform_type: int,
        names: list[str],
        *,
        preserve_current: bool = True,
    ) -> None:
        combo = self.platform_collections[platform_type]
        current = combo.currentText().strip() if preserve_current else ""
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("不选择合集", "")
        for name in names:
            combo.addItem(name, name)
        if current and current != "不选择合集":
            index = combo.findText(current)
            if index >= 0:
                combo.setCurrentIndex(index)
            else:
                combo.setEditText(current)
        combo.blockSignals(False)

    def _load_cached_collections(self, platform_type: int, *, preserve_current: bool = True) -> None:
        accounts = self._platform_accounts(platform_type)
        if not accounts:
            self._set_collection_options(platform_type, [], preserve_current=True)
            self._set_collection_status(platform_type, "请先勾选该平台账号")
            return
        by_account = collection_service.cached_collections(
            [int(account["id"]) for account in accounts],
            platform_type,
        )
        names = collection_service.common_collections(by_account)
        self._set_collection_options(platform_type, names, preserve_current=preserve_current)
        if names:
            account_text = f"{len(accounts)} 个账号共同" if len(accounts) > 1 else "当前账号"
            self._set_collection_status(platform_type, f"已加载{account_text}合集 {len(names)} 个", "success")
        elif isinstance(by_account, dict) and any(by_account.values()):
            self._set_collection_status(platform_type, "已缓存合集，但所选账号没有共同合集", "warning")
        else:
            self._set_collection_status(platform_type, "尚未同步")

    def _apply_platform_collection_values(self, values: dict) -> None:
        for key, value in values.items():
            platform_type = int(key)
            combo = self.platform_collections.get(platform_type)
            if not combo:
                continue
            collection_name = str(value or "").strip()
            if not collection_name:
                combo.setCurrentIndex(0)
                continue
            index = combo.findData(collection_name)
            if index < 0:
                index = combo.findText(collection_name)
            if index >= 0:
                combo.setCurrentIndex(index)
            else:
                combo.setEditText(collection_name)

    def _reload_selected_collection_caches(self) -> None:
        selected_types = {
            int(account.get("type") or 0)
            for account in self.selected_accounts()
        }
        for platform_type in self.platform_collections:
            if platform_type in selected_types:
                self._load_cached_collections(platform_type, preserve_current=True)
            else:
                self._set_collection_options(platform_type, [], preserve_current=True)
                self._set_collection_status(platform_type, "请先勾选该平台账号")

    def sync_platform_collections(self, platform_type: int) -> None:
        accounts = self._platform_accounts(platform_type)
        platform_name = account_service.PLATFORMS[platform_type]
        if not accounts:
            QMessageBox.information(self, "同步合集", f"请先在左侧勾选{platform_name}账号。")
            return

        task_key = f"collection-sync-{platform_type}"
        sync_button = self.platform_collection_buttons[platform_type]

        def on_started() -> None:
            sync_button.setEnabled(False)
            sync_button.setText("同步中")
            self._set_collection_status(platform_type, f"正在读取 {len(accounts)} 个账号的合集…")

        def on_success(result: dict) -> None:
            names = list(result.get("collections") or [])
            self._set_collection_options(platform_type, names, preserve_current=True)
            if names:
                account_text = f"{len(accounts)} 个账号共同" if len(accounts) > 1 else "当前账号"
                self._set_collection_status(
                    platform_type,
                    f"同步完成：{account_text}合集 {len(names)} 个",
                    "success",
                )
            else:
                self._set_collection_status(
                    platform_type,
                    "同步完成，但所选账号没有共同合集",
                    "warning",
                )

        def on_error(message: str) -> None:
            self._set_collection_status(platform_type, f"同步失败：{message}", "danger")
            QMessageBox.warning(self, "同步合集", f"{platform_name}合集同步失败：\n{message}")

        def on_finished() -> None:
            sync_button.setEnabled(True)
            sync_button.setText("同步合集")

        started = self.collection_tasks.run(
            task_key,
            lambda: collection_service.sync_accounts_collections(accounts),
            on_started=on_started,
            on_success=on_success,
            on_error=on_error,
            on_finished=on_finished,
        )
        if not started:
            self._set_collection_status(platform_type, "合集同步任务正在运行")

    def _set_douyin_location_status(
        self,
        text: str,
        role: str = "muted",
    ) -> None:
        if self.douyin_location_status is None:
            return
        self.douyin_location_status.setText(text)
        self.douyin_location_status.setProperty("role", role)
        self.douyin_location_status.style().unpolish(self.douyin_location_status)
        self.douyin_location_status.style().polish(self.douyin_location_status)

    def _douyin_location_text_edited(self, value: str) -> None:
        """用户改动搜索词后作废旧 POI，避免界面文字与任务地点不一致。"""

        self._douyin_selected_location = {}
        self._douyin_location_query = ""
        if self.douyin_location_results is not None:
            self.douyin_location_results.clear()
            self.douyin_location_results.setVisible(False)
        if self.douyin_location_results_title is not None:
            self.douyin_location_results_title.setVisible(False)
        self._douyin_location_search_timer.stop()
        keyword = " ".join(str(value or "").split())
        if not keyword:
            self._set_douyin_location_status("未添加定位")
            return
        if len(keyword) < douyin_location_service.MIN_KEYWORD_LENGTH:
            self._set_douyin_location_status("再输入 1 个字即可搜索官方地点")
            return
        self._set_douyin_location_status("等待搜索…")
        self._douyin_location_search_timer.start()

    def search_douyin_locations(self) -> None:
        """在后台读取抖音官方 POI 候选，不上传素材或创建平台内容。"""

        if self.douyin_location_keyword is None:
            return
        self._douyin_location_search_timer.stop()
        keyword = " ".join(self.douyin_location_keyword.text().split())
        try:
            keyword = douyin_location_service.normalize_location_keyword(keyword)
        except Exception as exc:
            self._set_douyin_location_status(str(exc), "warning")
            return

        accounts = self._platform_accounts(3)
        if len(accounts) != 1:
            message = (
                "请先勾选一个抖音账号再搜索地点"
                if not accounts
                else "同时选中了多个抖音账号，请保留一个后搜索地点"
            )
            self._set_douyin_location_status(message, "warning")
            return
        if self.location_tasks.is_running("douyin-location-search"):
            self._set_douyin_location_status("当前地点搜索尚未完成，已记录最新输入…")
            return

        account = dict(accounts[0])
        source_account_id = int(account.get("id") or 0)
        self._douyin_location_query = keyword
        search_button = self.douyin_location_search_button

        def on_started() -> None:
            if search_button:
                search_button.setEnabled(False)
                search_button.setText("搜索中")
            self._set_douyin_location_status(
                f"正在后台读取“{keyword}”的抖音官方地点…"
            )

        def on_success(rows: object) -> None:
            current = " ".join(self.douyin_location_keyword.text().split())
            current_accounts = {
                int(item.get("id") or 0)
                for item in self._platform_accounts(3)
            }
            if current != keyword or source_account_id not in current_accounts:
                return
            self._show_douyin_location_results(
                rows if isinstance(rows, list) else [],
                source_account_id=source_account_id,
            )

        def on_error(message: str) -> None:
            current = " ".join(self.douyin_location_keyword.text().split())
            if current == keyword:
                self._set_douyin_location_status(message, "danger")
                if self.douyin_location_results is not None:
                    self.douyin_location_results.clear()
                    self.douyin_location_results.setVisible(False)
                if self.douyin_location_results_title is not None:
                    self.douyin_location_results_title.setVisible(False)

        def on_finished() -> None:
            if search_button:
                search_button.setEnabled(True)
                search_button.setText("搜索")
            current = " ".join(self.douyin_location_keyword.text().split())
            if (
                current
                and current != keyword
                and len(current) >= douyin_location_service.MIN_KEYWORD_LENGTH
            ):
                self._douyin_location_search_timer.start()

        self.location_tasks.run(
            "douyin-location-search",
            lambda: douyin_location_service.search_douyin_locations(
                account,
                keyword,
            ),
            on_started=on_started,
            on_success=on_success,
            on_error=on_error,
            on_finished=on_finished,
        )

    def _show_douyin_location_results(
        self,
        rows: list[object],
        *,
        source_account_id: int,
    ) -> None:
        if self.douyin_location_results is None:
            return
        self.douyin_location_results.clear()
        valid_rows = []
        for value in rows:
            candidate = douyin_location_service.normalize_location_candidate(value)
            if not candidate:
                continue
            candidate["sourceAccountId"] = source_account_id
            valid_rows.append(candidate)
            address = candidate.get("address") or "平台未返回详细地址"
            tooltip = f"{candidate['name']}\n{address}"
            if candidate.get("distance"):
                tooltip += f"\n距离：{candidate['distance']}"
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, candidate)
            item.setToolTip(tooltip)
            item.setSizeHint(QSize(0, 82))
            self.douyin_location_results.addItem(item)
            self.douyin_location_results.setItemWidget(
                item,
                self._douyin_location_candidate_widget(candidate),
            )
        self.douyin_location_results.setVisible(bool(valid_rows))
        if self.douyin_location_results_title is not None:
            self.douyin_location_results_title.setVisible(bool(valid_rows))
        if valid_rows:
            self._set_douyin_location_status(
                f"找到 {len(valid_rows)} 个官方地点，请明确选择一项",
                "success",
            )
        else:
            self._set_douyin_location_status(
                "未找到匹配地点，请尝试更完整的地点名称",
                "warning",
            )

    @staticmethod
    def _douyin_location_candidate_widget(candidate: dict[str, object]) -> QWidget:
        """以双行信息卡完整呈现地点，避免 Qt 默认委托省略地址。"""

        card = QWidget()
        card.setObjectName("douyinLocationCandidateCard")
        card.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)

        heading = QWidget()
        heading.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        heading_layout = QHBoxLayout(heading)
        heading_layout.setContentsMargins(0, 0, 0, 0)
        heading_layout.setSpacing(10)

        name = QLabel(str(candidate.get("name") or "未命名地点"))
        name.setObjectName("douyinLocationName")
        name.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        heading_layout.addWidget(name, 1)

        distance_text = str(candidate.get("distance") or "").strip()
        if distance_text:
            distance = QLabel(distance_text)
            distance.setObjectName("douyinLocationDistance")
            distance.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents,
                True,
            )
            heading_layout.addWidget(distance, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(heading)

        address = QLabel(
            str(candidate.get("address") or "平台未返回详细地址")
        )
        address.setObjectName("douyinLocationAddress")
        address.setWordWrap(True)
        address.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        address.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(address)
        return card

    def _select_douyin_location_item(self, item: QListWidgetItem) -> None:
        candidate = douyin_location_service.normalize_location_candidate(
            item.data(Qt.ItemDataRole.UserRole)
        )
        if not candidate or not self.douyin_location_keyword:
            self._set_douyin_location_status("当前地点候选数据无效，请重新搜索", "danger")
            return
        raw = item.data(Qt.ItemDataRole.UserRole) or {}
        candidate["sourceAccountId"] = int(raw.get("sourceAccountId") or 0)
        self._douyin_selected_location = candidate
        self.douyin_location_keyword.blockSignals(True)
        self.douyin_location_keyword.setText(candidate["name"])
        self.douyin_location_keyword.blockSignals(False)
        if self.douyin_location_results is not None:
            self.douyin_location_results.setVisible(False)
        if self.douyin_location_results_title is not None:
            self.douyin_location_results_title.setVisible(False)
        detail = candidate.get("address") or "平台未返回详细地址"
        if candidate.get("distance"):
            detail += f"  ·  {candidate['distance']}"
        self._set_douyin_location_status(
            f"已选择：{candidate['name']}\n{detail}",
            "success",
        )

    def _ensure_douyin_location_account_consistency(self) -> None:
        if not self._douyin_selected_location:
            return
        selected_ids = {
            int(account.get("id") or 0)
            for account in self._platform_accounts(3)
        }
        source_id = int(self._douyin_selected_location.get("sourceAccountId") or 0)
        if source_id in selected_ids:
            return
        self._douyin_selected_location = {}
        if self.douyin_location_results is not None:
            self.douyin_location_results.clear()
            self.douyin_location_results.setVisible(False)
        if self.douyin_location_results_title is not None:
            self.douyin_location_results_title.setVisible(False)
        self._set_douyin_location_status(
            "抖音账号已变更，请重新搜索并选择发布定位",
            "warning",
        )

    def refresh_platform_navigation(self) -> None:
        current_item = self.platform_nav.currentItem()
        current_type = int(current_item.data(Qt.ItemDataRole.UserRole) or 0) if current_item else 0
        account_counts: dict[int, int] = {}
        for account in self.selected_accounts():
            platform_type = int(account.get("type") or 0)
            if platform_type in account_service.PLATFORMS:
                account_counts[platform_type] = account_counts.get(platform_type, 0) + 1
        ordered_types = list(account_service.PLATFORM_ORDER)

        self.platform_nav.blockSignals(True)
        self.platform_nav.clear()
        selected_row = -1
        for row, platform_type in enumerate(ordered_types):
            account_count = account_counts.get(platform_type, 0)
            is_target = account_count > 0
            name = account_service.PLATFORMS[platform_type]
            item = QListWidgetItem(
                f"{name}  ·  {account_count}" if is_target else name
            )
            item.setData(Qt.ItemDataRole.UserRole, platform_type)
            item.setData(PLATFORM_TARGET_ROLE, is_target)
            item.setSizeHint(QSize(0, 40))
            item.setToolTip(
                f"{name}：本次发布对象，已选择 {account_count} 个账号"
                if is_target
                else f"{name}：未选择发布账号"
            )
            font = item.font()
            font.setBold(is_target)
            item.setFont(font)
            item.setForeground(QColor("#0F766E" if is_target else "#98A2B3"))
            item.setBackground(QColor("#E7F5F2" if is_target else "#FFFFFF"))
            self.platform_nav.addItem(item)
            self._set_platform_editor_target_state(platform_type, is_target)
            if platform_type == current_type and (is_target or not account_counts):
                selected_row = row
        self.platform_nav.blockSignals(False)

        if selected_row < 0 and account_counts:
            first_target = next(
                (
                    row
                    for row, platform_type in enumerate(ordered_types)
                    if platform_type in account_counts
                ),
                0,
            )
            selected_row = first_target
        self.platform_nav.setCurrentRow(selected_row if selected_row >= 0 else 0)
        self._platform_nav_changed(self.platform_nav.currentItem(), None)

    def _right_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("publishActivityPanel")
        panel.setProperty("workspace", True)
        _side_width, side_min_width = publish_activity_widths(sys.platform)
        panel.setMinimumWidth(side_min_width)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(9)

        media_header = QHBoxLayout()
        self.media_title = QLabel("视频素材")
        self.media_title.setProperty("role", "sectionTitle")
        media_header.addWidget(self.media_title)
        media_header.addStretch()
        self.selected_media_label = QLabel("已选择素材：0")
        self.selected_media_label.setProperty("role", "caption")
        media_header.addWidget(self.selected_media_label)
        layout.addLayout(media_header)

        self.media_actions_widget = QWidget()
        media_actions = QHBoxLayout(self.media_actions_widget)
        media_actions.setContentsMargins(0, 0, 0, 0)
        media_actions.setSpacing(4)
        media_actions.addStretch()
        select_media = button("全选", variant="ghost", compact=True)
        select_media.clicked.connect(lambda: self._set_list_checked(self.media_list, True))
        media_actions.addWidget(select_media)
        self.deselect_all_media_btn = button("取消全选", variant="ghost", compact=True)
        self.deselect_all_media_btn.clicked.connect(
            lambda: self._set_list_checked(self.media_list, False)
        )
        media_actions.addWidget(self.deselect_all_media_btn)
        layout.addWidget(self.media_actions_widget)

        self.media_search = QLineEdit()
        self.media_search.setPlaceholderText("搜索视频素材名称")
        self.media_search.textChanged.connect(
            lambda _text: self.refresh_media()
        )
        layout.addWidget(self.media_search)

        self.text_material_hint = QFrame()
        self.text_material_hint.setProperty("subPanel", True)
        text_material_layout = QVBoxLayout(self.text_material_hint)
        text_material_layout.setContentsMargins(12, 12, 12, 12)
        text_material_layout.setSpacing(6)
        text_material_title = QLabel("纯文字无需素材")
        text_material_title.setProperty("role", "sectionTitle")
        text_material_layout.addWidget(text_material_title)
        text_material_description = QLabel(
            "这一类发布需要填写标题、正文、声明、定时与封面。"
            "抖音、B站和公众号都必须在中间选择一张封面。"
        )
        text_material_description.setProperty("role", "muted")
        text_material_description.setWordWrap(True)
        text_material_layout.addWidget(text_material_description)
        self.text_material_hint.setVisible(False)
        layout.addWidget(self.text_material_hint)

        self.media_list = QListWidget()
        self.media_list.setObjectName("mediaSelectionList")
        self.media_list.setMinimumHeight(180)
        self.media_list.setIconSize(QSize(112, 63))
        self.media_list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.media_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.media_list.customContextMenuRequested.connect(self.open_media_menu)
        self.media_list.itemChanged.connect(self._media_item_changed)
        self.media_list.itemDoubleClicked.connect(self.preview_media_item)
        layout.addWidget(self.media_list, 3)

        log_header = QHBoxLayout()
        log_title = QLabel("执行日志")
        log_title.setProperty("role", "sectionTitle")
        log_header.addWidget(log_title)
        log_header.addStretch()
        self.copy_log_btn = button("复制日志", variant="ghost", compact=True)
        self.copy_log_btn.setToolTip("一键复制全部执行日志")
        self.copy_log_btn.clicked.connect(self.copy_execution_log)
        log_header.addWidget(self.copy_log_btn)
        clear_log = button("清空", variant="ghost", compact=True)
        clear_log.clicked.connect(lambda: self.log.clear())
        log_header.addWidget(clear_log)
        layout.addLayout(log_header)
        self.log = QTextEdit()
        self.log.setObjectName("executionLog")
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("暂无执行记录")
        layout.addWidget(self.log, 2)
        return panel

    def copy_execution_log(self) -> None:
        text = self.log.toPlainText()
        if not text.strip():
            self.copy_log_btn.setText("暂无日志")
        else:
            QApplication.clipboard().setText(text)
            self.copy_log_btn.setText("已复制")
        QTimer.singleShot(1500, self._reset_copy_log_button)

    def _reset_copy_log_button(self) -> None:
        self.copy_log_btn.setText("复制日志")

    def refresh(self, force: bool = False) -> None:
        media_rows = media_service.list_media()
        self.refresh_templates()
        self.refresh_tags()
        self.refresh_accounts()
        self.refresh_media_categories(media_rows)
        self.refresh_media(media_rows)
        self.refresh_covers(media_rows, force=force)
        self.refresh_saved_content_status()

    def refresh_templates(self) -> None:
        self.template_combo.clear()
        self.template_combo.addItem("不使用模板", None)
        for item in publish_config_service.list_templates():
            self.template_combo.addItem(item["name"], item["id"])

    def refresh_tags(self) -> None:
        self.tag_history.clear()
        for tag in publish_config_service.list_tags():
            self._append_history_tag(tag)

    def _append_history_tag(self, tag: str) -> None:
        item = QListWidgetItem(tag)
        chip = QFrame()
        chip.setObjectName("historyTagChip")
        chip_layout = QHBoxLayout(chip)
        chip_layout.setContentsMargins(7, 3, 4, 3)
        chip_layout.setSpacing(3)

        tag_button = QToolButton()
        tag_button.setObjectName("historyTagText")
        display_text = f"#{tag}"
        tag_button.setText(display_text)
        tag_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        tag_button.setAutoRaise(True)
        tag_button.setCursor(Qt.CursorShape.PointingHandCursor)
        tag_button.setToolTip(f"添加 #{tag}")
        tag_button.setAccessibleName(f"添加历史话题 #{tag}")
        tag_button.clicked.connect(lambda _checked=False, value=tag: self.add_tag(value))
        tag_button.ensurePolished()
        tag_text_width = tag_button.fontMetrics().horizontalAdvance(display_text)
        # Windows 原生 QToolButton 的 sizeHint 会为纯文字按钮预留过多空间；
        # 使用实际文字宽度加安全留白，既不省略文字，也能让常用话题紧凑换行。
        tag_button_width = tag_text_width + 14
        tag_button.setFixedWidth(tag_button_width)
        chip_layout.addWidget(tag_button)

        delete_button = QToolButton()
        delete_button.setObjectName("historyTagDelete")
        delete_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_TitleBarCloseButton)
        )
        delete_button.setIconSize(QSize(7, 7))
        delete_button.setAutoRaise(True)
        delete_button.setFixedSize(12, 12)
        delete_button.setCursor(Qt.CursorShape.PointingHandCursor)
        delete_button.setToolTip(f"删除 #{tag}")
        delete_button.setAccessibleName(f"删除历史话题 #{tag}")
        delete_button.clicked.connect(lambda _checked=False, value=tag: self.delete_history_tag(value))
        chip_layout.addWidget(delete_button, 0, Qt.AlignmentFlag.AlignVCenter)

        chip_width = max(70, tag_button_width + 26)
        chip.setFixedSize(chip_width, 30)
        item.setSizeHint(QSize(chip_width, 30))
        self.tag_history.addItem(item)
        self.tag_history.setItemWidget(item, chip)

    def delete_history_tag(self, tag: str) -> None:
        publish_config_service.delete_tag(tag)
        self.refresh_tags()

    def refresh_accounts(self) -> None:
        accounts = account_service.list_accounts()
        self._account_rows = accounts
        valid_ids = {int(item["id"]) for item in accounts}
        self._selected_account_ids.intersection_update(valid_ids)
        current_profile = self.profile_filter.currentText()
        profiles = ["全部主体"] + sorted({item["profileName"] for item in accounts})
        self.profile_filter.blockSignals(True)
        self.profile_filter.clear()
        self.profile_filter.addItems(profiles)
        if current_profile in profiles:
            self.profile_filter.setCurrentText(current_profile)
        self.profile_filter.blockSignals(False)

        current_platform = self.platform_filter.currentData()
        self.platform_filter.blockSignals(True)
        self.platform_filter.clear()
        self.platform_filter.addItem("全部平台", None)
        allowed_platform_types = self._available_platform_types()
        for platform_type in account_service.PLATFORM_ORDER:
            if platform_type not in allowed_platform_types:
                continue
            self.platform_filter.addItem(account_service.PLATFORMS[platform_type], platform_type)
        if current_platform is not None:
            index = self.platform_filter.findData(current_platform)
            if index >= 0:
                self.platform_filter.setCurrentIndex(index)
        self.platform_filter.blockSignals(False)

        profile = self.profile_filter.currentText()
        platform_type = self.platform_filter.currentData()
        self.account_list.blockSignals(True)
        self.account_list.clear()
        for account in accounts:
            if int(account["type"]) not in allowed_platform_types:
                continue
            if profile != "全部主体" and account["profileName"] != profile:
                continue
            if platform_type is not None and account["type"] != platform_type:
                continue
            remark = str(account.get("remark") or "").strip()
            display_parts = [
                account["platformName"],
                account["profileName"],
                account["userName"] or "未命名账号",
                account["statusText"],
            ]
            if remark:
                display_parts.append(f"备注：{remark}")
            text = " · ".join(display_parts)
            tooltip_lines = [
                f"平台：{account['platformName']}",
                f"主体：{account['profileName']}",
                f"账号名：{account['userName'] or '未命名账号'}",
                f"状态：{account['statusText']}",
                "通道：一键发本地浏览器会话",
            ]
            if remark:
                tooltip_lines.append(f"备注：{remark}")
            tooltip = "\n".join(tooltip_lines)
            item = QListWidgetItem(text)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            account_id = int(account["id"])
            item.setCheckState(
                Qt.CheckState.Checked
                if account_id in self._selected_account_ids
                else Qt.CheckState.Unchecked
            )
            item.setData(Qt.ItemDataRole.UserRole, account)
            item.setToolTip(tooltip)
            foreground = {
                "normal": "#344054",
                "stale": "#B54708",
                "abnormal": "#B42318",
            }.get(account.get("healthStatus"), "#B42318")
            item.setForeground(QColor(foreground))
            item.setSizeHint(QSize(0, 40))
            self.account_list.addItem(item)
        self.account_list.blockSignals(False)
        self.update_selected_labels()
        self._reload_selected_collection_caches()

    def open_account_menu(self, pos) -> None:
        item = self.account_list.itemAt(pos)
        if not item:
            return
        self.account_list.setCurrentItem(item)
        account = item.data(Qt.ItemDataRole.UserRole) or {}
        if not account:
            return

        menu = self._build_account_context_menu(account)
        menu.exec(self.account_list.viewport().mapToGlobal(pos))

    def _build_account_context_menu(self, account: dict) -> QMenu:
        menu = QMenu(self)
        menu.addAction(
            "打开后台",
            lambda _checked=False, row=account: self.open_account_backend(row),
        )
        menu.addAction(
            "检测登录",
            lambda _checked=False, row=account: self.check_account_login(row),
        )
        menu.addAction(
            "重新登录",
            lambda _checked=False, row=account: self.relogin_account(row),
        )
        return menu

    def open_account_backend(self, account: dict) -> None:
        reused = account_browser_service.open_account_backend(account)
        platform_name = account.get("platformName") or account_service.PLATFORMS.get(
            int(account.get("type") or 0),
            "平台",
        )
        message = (
            f"已切换到现有后台：{platform_name}"
            if reused
            else f"已打开后台：{platform_name}"
        )
        self.account_health_label.setText(message)
        self.log.append(f"[info] {message}")

    def check_account_login(self, account: dict) -> None:
        account_id = int(account.get("id") or 0)
        if not account_id:
            return
        task_key = f"publish_account_check_{account_id}"
        if self.account_health_tasks.is_running(task_key):
            self.account_health_label.setText("账号状态：检测中")
            return

        def on_success(payload: dict) -> None:
            self.refresh_accounts()
            checked = list(payload.get("checked") or [])
            status_text = (
                checked[0].get("statusText")
                if checked
                else "检测完成"
            )
            self.account_health_label.setText(f"账号状态：{status_text}")
            self.log.append(
                f"[info] {account.get('platformName') or '平台'}登录检测完成：{status_text}"
            )
            self._present_account_login_intervention(payload)

        self.account_health_tasks.run(
            task_key,
            lambda: account_service.validate_accounts([account_id]),
            on_started=lambda: self.account_health_label.setText("账号状态：检测中"),
            on_success=on_success,
            on_error=self._show_account_health_error,
        )

    def _present_account_login_intervention(self, payload: dict) -> bool:
        """检测正常时保持静默，异常时只提示手动重新登录。"""

        rows = list(payload.get("interventionRequired") or [])
        if not rows:
            return False
        first = rows[0]
        platform = str(first.get("platformName") or "平台")
        account_name = str(first.get("userName") or first.get("profileName") or "账号")
        message = (
            f"{platform} | {account_name} 未通过后台登录检测。\n\n"
            "本次检测只更新账号状态，不会打开平台登录页。\n"
            "如需扫码、验证码或恢复会话，请使用账号操作中的“重新登录”。"
        )
        self.account_health_label.setText("账号状态：需用户处理")
        self.log.append(f"[warning] {message}")
        QMessageBox.warning(self, "登录状态需处理", message)
        return True

    def relogin_account(self, account: dict) -> None:
        dialog = LoginDialog(
            self,
            account,
            background_login=True,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh_accounts()

    def refresh_media(self, media_rows: list[dict] | None = None) -> None:
        keyword = self.media_search.text().strip().lower()
        rows = media_rows if media_rows is not None else media_service.list_media()
        category = self.media_category_combo.currentData() if hasattr(self, "media_category_combo") else "全部"
        material_type = {"video": "视频", "article": "图片"}.get(self.content_type)
        self._media_rows = (
            [
                item for item in rows
                if item["typeText"] == material_type
                and (category in (None, "全部") or item.get("mediaCategory") == category)
            ]
            if material_type
            else []
        )
        valid_ids = {int(item["id"]) for item in self._media_rows}
        self._selected_media_ids.intersection_update(valid_ids)
        self.media_list.blockSignals(True)
        self.media_list.clear()
        for media in self._media_rows:
            remark = media.get("remark") or ""
            searchable = f"{media['filename']} {remark}".lower()
            if keyword and keyword not in searchable:
                continue
            size = media.get("filesize")
            size_text = f"{size}MB" if size not in (None, "") else "未记录大小"
            remark_text = f" · {remark}" if remark else ""
            text = f"{media['filename']}\n{size_text}{remark_text}"
            item = QListWidgetItem(text)
            cover_path = media_service.cover_display_path(media)
            if cover_path:
                item.setIcon(QIcon(cover_path))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            media_id = int(media["id"])
            item.setCheckState(
                Qt.CheckState.Checked
                if media_id in self._selected_media_ids
                else Qt.CheckState.Unchecked
            )
            item.setData(Qt.ItemDataRole.UserRole, media)
            item.setToolTip(f"{text}\n存储位置：{media.get('storedPath') or ''}")
            item.setSizeHint(QSize(0, 76 if self.content_type == "video" else 64))
            self.media_list.addItem(item)
        self.media_list.blockSignals(False)
        self.update_selected_labels()

    def refresh_media_categories(self, media_rows: list[dict] | None = None) -> None:
        """与素材管理共用主体分类；切换后发布中心只列出该分类素材。"""

        del media_rows
        current = self.media_category_combo.currentData()
        categories = media_service.list_categories()
        self.media_category_combo.blockSignals(True)
        self.media_category_combo.clear()
        for category in categories:
            self.media_category_combo.addItem(
                f"{category['name']}（{category['count']}）",
                category["name"],
            )
        index = self.media_category_combo.findData(current)
        self.media_category_combo.setCurrentIndex(index if index >= 0 else 0)
        self.media_category_combo.blockSignals(False)

    def _media_category_changed(self) -> None:
        self.refresh_media()
        self.update_selected_labels()

    def refresh_covers(
        self,
        media_rows: list[dict] | None = None,
        *,
        force: bool = False,
    ) -> None:
        rows = media_rows if media_rows is not None else media_service.list_media()
        images = [item for item in rows if item["typeText"] == "图片"]
        signature = self._cover_signature(images)
        if not force and signature == self._cover_rows_signature:
            return
        self._cover_rows_signature = signature
        active_paths = {
            path
            for image in images
            if (path := self._cover_preview_path(image.get("file_path")))
        }
        self._cover_pixmap_cache = {
            path: cached
            for path, cached in self._cover_pixmap_cache.items()
            if path in active_paths
        }
        for combo in (self.cover_common, self.cover_43, self.cover_34):
            current = combo.currentData()
            combo.clear()
            combo.addItem("不指定", "")
            for image in images:
                if not self._cover_matches_selector(combo, image.get("file_path")):
                    continue
                combo.addItem(self.cover_option_text(image), image["file_path"])
            if current:
                index = combo.findData(current)
                if index >= 0:
                    combo.setCurrentIndex(index)
            self.update_cover_preview(combo)
        platform_combos = (
            list(self.platform_cover_34.values())
            + list(self.platform_cover_43.values())
        )
        for combo in platform_combos:
            current = combo.currentData()
            combo.clear()
            combo.addItem("使用通用封面", "")
            for image in images:
                if not self._cover_matches_selector(combo, image.get("file_path")):
                    continue
                combo.addItem(self.cover_option_text(image), image["file_path"])
            if current:
                index = combo.findData(current)
                if index >= 0:
                    combo.setCurrentIndex(index)
        self._refresh_platform_cover_previews()
        self.update_cover_summary()

    def _cover_signature(self, images: list[dict]) -> tuple:
        rows = []
        for image in images:
            path = self._cover_preview_path(image.get("file_path"))
            file_signature = (path, 0, 0)
            if path:
                try:
                    stat = Path(path).stat()
                    file_signature = (path, stat.st_mtime_ns, stat.st_size)
                except OSError:
                    pass
            rows.append(
                (
                    image.get("id"),
                    image.get("filename"),
                    image.get("file_path"),
                    image.get("remark"),
                    file_signature,
                )
            )
        return tuple(rows)

    def _cached_cover_pixmap(self, stored_name: str | None) -> QPixmap:
        path = self._cover_preview_path(stored_name)
        if not path:
            return QPixmap()
        try:
            stat = Path(path).stat()
            file_signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return QPixmap()
        cached = self._cover_pixmap_cache.get(path)
        if cached and cached[:2] == file_signature:
            return cached[2]
        pixmap = QPixmap(path)
        self._cover_pixmap_cache[path] = (*file_signature, pixmap)
        return pixmap

    def _cover_matches_selector(self, combo: QComboBox, stored_name: str | None) -> bool:
        orientation = combo.property("coverOrientation")
        if orientation not in {"portrait", "landscape"}:
            return True
        pixmap = self._cached_cover_pixmap(stored_name)
        if pixmap.isNull():
            return False
        ratio = pixmap.width() / max(1, pixmap.height())
        if orientation == "portrait":
            return abs(ratio - (3 / 4)) <= 0.06
        return abs(ratio - (4 / 3)) <= 0.08

    def cover_option_text(self, image: dict) -> str:
        parts = [image.get("filename") or ""]
        size_text = self.cover_image_size_text(image.get("file_path"))
        if size_text:
            parts.append(size_text)
        if image.get("remark"):
            parts.append(f"备注：{image['remark']}")
        return " | ".join(part for part in parts if part)

    def cover_image_size_text(self, stored_name: str | None) -> str:
        pixmap = self._cached_cover_pixmap(stored_name)
        if pixmap.isNull():
            return ""
        return f"{pixmap.width()}x{pixmap.height()}"

    def update_cover_preview(self, combo: QComboBox) -> None:
        label = self.cover_previews.get(combo)
        if not label:
            return
        path = self._cover_preview_path(combo.currentData())
        if not path:
            label.set_preview(None)
            if combo in (self.cover_34, self.cover_43):
                self._refresh_platform_cover_previews()
            return
        pixmap = self._cached_cover_pixmap(combo.currentData())
        if pixmap.isNull():
            label.set_preview(None, "无法预览")
            if combo in (self.cover_34, self.cover_43):
                self._refresh_platform_cover_previews()
            return
        label.set_preview(pixmap)
        if combo in (self.cover_34, self.cover_43):
            self._refresh_platform_cover_previews()
        self.update_cover_summary()

    def _update_platform_cover_preview(
        self,
        platform_type: int,
        ratio: str,
    ) -> None:
        preview = self.platform_cover_previews.get((platform_type, ratio))
        if not preview:
            return
        combo = (
            self.platform_cover_34[platform_type]
            if ratio == "3:4"
            else self.platform_cover_43[platform_type]
        )
        common_combo = self.cover_34 if ratio == "3:4" else self.cover_43
        selected_path = combo.currentData()
        effective_path = selected_path or common_combo.currentData()
        path = self._cover_preview_path(effective_path)
        if not path:
            preview.set_preview(None, "使用通用封面")
            return
        pixmap = self._cached_cover_pixmap(effective_path)
        if pixmap.isNull():
            preview.set_preview(None, "无法预览")
            return
        preview.set_preview(pixmap)

    def _refresh_platform_cover_previews(self, *_args) -> None:
        for platform_type in self.platform_cover_34:
            self._update_platform_cover_preview(platform_type, "3:4")
            self._update_platform_cover_preview(platform_type, "4:3")

    def update_cover_summary(self, *_args) -> None:
        if not hasattr(self, "cover_summary_label"):
            return
        choices = [
            ("竖版", self.cover_34.currentText()),
            ("横版", self.cover_43.currentText()),
        ]
        selected = [(label, text) for label, text in choices if text and text != "不指定"]
        if not selected:
            self.cover_summary_label.setText("未指定")
            self.cover_summary_label.setToolTip("")
            return
        self.cover_summary_label.setText("、".join(label for label, _text in selected) + "已设置")
        self.cover_summary_label.setToolTip(
            "\n".join(f"{label}：{text}" for label, text in selected)
        )

    def _cover_preview_path(self, stored_name: str | None) -> str:
        if not stored_name:
            return ""
        path = Path(stored_name)
        if not path.is_absolute():
            path = VIDEO_DIR / path.name
        return str(path) if path.exists() else ""

    def update_selected_labels(self) -> None:
        account_count = len(self._selected_account_ids)
        media_count = len(self._selected_media_ids)
        self.selected_account_label.setText(f"已选择 {account_count} 个")
        if self.content_type == "text":
            self.selected_media_label.setText("无需素材")
        else:
            self.selected_media_label.setText(f"已选择 {media_count} 个")
        type_label = {"video": "视频", "article": "图文", "text": "文字"}.get(
            self.content_type, "视频"
        )
        self.selection_summary.setText(
            f"{account_count} 个账号 · {media_count} 个素材 · {type_label}"
        )
        self._update_account_health_label()
        if hasattr(self, "platform_nav"):
            self.refresh_platform_navigation()
        if account_count:
            QTimer.singleShot(300, self.check_selected_account_health)

    def _update_account_health_label(self) -> None:
        selected = self.selected_accounts()
        if not selected:
            text = "账号状态：未选择"
            role = "muted"
        else:
            stale = sum(1 for row in selected if row.get("healthStatus") == "stale")
            abnormal = sum(1 for row in selected if row.get("healthStatus") == "abnormal")
            if abnormal:
                text = f"账号状态：异常 {abnormal}"
                role = "warning"
            elif stale:
                text = f"账号状态：待检测 {stale}"
                role = "warning"
            else:
                text = "账号状态：已验证"
                role = "success"
        self.account_health_label.setText(text)
        self.account_health_label.setProperty("role", role)
        self.account_health_label.style().unpolish(self.account_health_label)
        self.account_health_label.style().polish(self.account_health_label)

    def check_selected_account_health(self) -> None:
        if not self._selected_account_ids:
            self._update_account_health_label()
            return
        stale_ids = account_service.accounts_requiring_check(
            self._selected_account_ids
        )
        if not stale_ids or self.account_health_tasks.is_running("publish_account_health"):
            self._update_account_health_label()
            return

        self.account_health_tasks.run(
            "publish_account_health",
            lambda: account_service.validate_accounts(stale_ids),
            on_started=lambda: self.account_health_label.setText("账号状态：检测中"),
            on_success=self._finish_selected_account_health,
            on_error=self._show_account_health_error,
        )

    def _finish_selected_account_health(self, payload: dict) -> None:
        self.refresh_accounts()
        self._present_account_login_intervention(payload)

    def _show_account_health_error(self, _message: str) -> None:
        self.account_health_label.setText("账号状态：检测失败")
        self.account_health_label.setProperty("role", "warning")
        self.account_health_label.style().unpolish(self.account_health_label)
        self.account_health_label.style().polish(self.account_health_label)

    def _account_item_changed(self, item: QListWidgetItem) -> None:
        account = item.data(Qt.ItemDataRole.UserRole) or {}
        account_id = int(account.get("id") or 0)
        if not account_id:
            return
        if item.checkState() == Qt.CheckState.Checked:
            self._selected_account_ids.add(account_id)
        else:
            self._selected_account_ids.discard(account_id)
        self.update_selected_labels()
        self._reload_selected_collection_caches()
        self._ensure_douyin_location_account_consistency()

    def _media_item_changed(self, item: QListWidgetItem) -> None:
        media = item.data(Qt.ItemDataRole.UserRole) or {}
        media_id = int(media.get("id") or 0)
        if not media_id:
            return
        if item.checkState() == Qt.CheckState.Checked:
            self._selected_media_ids.add(media_id)
        else:
            self._selected_media_ids.discard(media_id)
        self.update_selected_labels()

    def _set_list_checked(self, widget: QListWidget, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        widget.blockSignals(True)
        for index in range(widget.count()):
            item = widget.item(index)
            item.setCheckState(state)
            row = item.data(Qt.ItemDataRole.UserRole) or {}
            row_id = int(row.get("id") or 0)
            target = self._selected_account_ids if widget is self.account_list else self._selected_media_ids
            if checked:
                target.add(row_id)
            else:
                target.discard(row_id)
        widget.blockSignals(False)
        self.update_selected_labels()
        if widget is self.account_list:
            self._reload_selected_collection_caches()

    def selected_accounts(self) -> list[dict]:
        return [
            row
            for row in self._account_rows
            if int(row.get("id") or 0) in self._selected_account_ids
        ]

    def selected_media(self) -> list[dict]:
        return [
            row
            for row in self._media_rows
            if int(row.get("id") or 0) in self._selected_media_ids
        ]

    def preview_media_item(self, item: QListWidgetItem) -> None:
        self.preview_media(item.data(Qt.ItemDataRole.UserRole) or {})

    def clear_edited_content(self) -> None:
        """清空当前内容表单，同时保留账号、素材、日志和已保存快照。"""

        answer = QMessageBox.question(
            self,
            "清空已编辑内容",
            "确定清空通用内容和全部平台适配内容吗？\n\n"
            "已选择的账号、视频素材、执行日志和已保存内容不会被删除。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self.template_combo.setCurrentIndex(0)
        self.common_title_input.clear()
        self.title_input.clear()
        self.tags_input.clear()
        self.original_declaration.setChecked(False)
        self.ai_generated_content.setChecked(False)
        self._ai_declaration_explicitly_confirmed = False
        public_index = self.common_visibility.findData("public")
        self.common_visibility.setCurrentIndex(public_index if public_index >= 0 else 0)
        self.common_schedule_enabled.setChecked(False)
        self.common_schedule_date.setDate(QDate.currentDate().addDays(1))
        self.common_schedule_time.setTime(QTime(18, 0))
        self._sync_common_schedule_values()

        for combo in (self.cover_common, self.cover_34, self.cover_43):
            combo.setCurrentIndex(0)
        for editor in self.platform_titles.values():
            editor.clear()
        for editor in self.platform_texts.values():
            editor.clear()
        for editor in self.platform_tags.values():
            editor.clear()
        for combo in self.platform_collections.values():
            combo.setCurrentIndex(0)
        for platform_type, enabled in self.platform_schedule_enabled.items():
            enabled.setChecked(False)
            self.platform_schedule_dates[platform_type].setDate(
                QDate.currentDate().addDays(1)
            )
            self.platform_schedule_times[platform_type].setTime(QTime(18, 0))
        for combo in self.platform_cover_34.values():
            combo.setCurrentIndex(0)
        for combo in self.platform_cover_43.values():
            combo.setCurrentIndex(0)
        for combo in self.platform_categories.values():
            combo.setCurrentIndex(0)
        for combo in self.platform_visibility.values():
            combo.setCurrentIndex(0)
        self.bili_partition.setCurrentIndex(0)
        self.bili_type.setCurrentIndex(0)
        if self.douyin_sync_toutiao:
            self.douyin_sync_toutiao.setChecked(False)
        if self.douyin_location_keyword is not None:
            self.douyin_location_keyword.blockSignals(True)
            self.douyin_location_keyword.clear()
            self.douyin_location_keyword.blockSignals(False)
        self._douyin_selected_location = {}
        self._douyin_location_search_timer.stop()
        if self.douyin_location_results is not None:
            self.douyin_location_results.clear()
            self.douyin_location_results.setVisible(False)
        if self.douyin_location_results_title is not None:
            self.douyin_location_results_title.setVisible(False)
        self._set_douyin_location_status("未添加定位")
        if self.wechat_group_notification:
            self.wechat_group_notification.setChecked(True)
        if self.youtube_made_for_kids:
            self.youtube_made_for_kids.setChecked(False)
        if self.youtube_notify_subscribers:
            self.youtube_notify_subscribers.setChecked(True)
        if self.instagram_share_to_feed:
            self.instagram_share_to_feed.setChecked(True)

        self._refresh_platform_cover_previews()
        self.update_cover_summary()
        self.content_tabs.setCurrentIndex(0)
        self.content_save_status.setText("当前填写已清空，可用“恢复内容”找回已保存内容")
        self.content_save_status.setProperty("role", "muted")
        self.content_save_status.style().unpolish(self.content_save_status)
        self.content_save_status.style().polish(self.content_save_status)

    def open_media_menu(self, pos) -> None:
        item = self.media_list.itemAt(pos)
        if not item:
            return
        self.media_list.setCurrentItem(item)
        media = item.data(Qt.ItemDataRole.UserRole) or {}
        if not media:
            return
        menu = self._build_media_context_menu(media)
        menu.exec(self.media_list.viewport().mapToGlobal(pos))

    def _build_media_context_menu(self, media: dict) -> QMenu:
        return build_media_context_menu(
            self,
            media,
            preview=self.preview_media,
            rename=self.rename_media,
            edit_remark=self.edit_media_remark,
            refresh_cover=self.refresh_media_cover,
            open_folder=self.open_media_folder,
            delete=self.delete_media,
        )

    def preview_media(self, media: dict) -> None:
        path = Path(media.get("storedPath") or "")
        if not path.exists():
            QMessageBox.warning(self, "预览素材", "素材文件不存在，请在素材管理中重新导入。")
            return
        open_path(path)

    def rename_media(self, media: dict) -> None:
        text, ok = QInputDialog.getText(
            self,
            "重命名素材",
            "文件名：",
            text=str(media.get("filename") or ""),
        )
        if ok and text.strip():
            media_service.rename_media(int(media["id"]), text)
            self.refresh(force=True)

    def edit_media_remark(self, media: dict) -> None:
        text, ok = QInputDialog.getText(
            self,
            "编辑备注",
            "备注：",
            text=str(media.get("remark") or ""),
        )
        if ok:
            media_service.update_remark(int(media["id"]), text)
            self.refresh(force=True)

    def refresh_media_cover(self, media: dict) -> None:
        count = media_service.refresh_covers([int(media["id"])])
        QMessageBox.information(
            self,
            "刷新封面",
            "封面刷新完成" if count else "当前素材不是视频，或暂时无法生成封面",
        )
        self.refresh(force=True)

    def open_media_folder(self, media: dict) -> None:
        path = Path(media.get("storedPath") or "")
        folder = path.parent if path.parent.exists() else ROOT_DIR
        reveal_in_folder(folder)

    def delete_media(self, media: dict) -> None:
        filename = str(media.get("filename") or "当前素材")
        if (
            QMessageBox.question(self, "删除素材", f"确定删除 {filename}？")
            != QMessageBox.StandardButton.Yes
        ):
            return
        media_id = int(media["id"])
        media_service.delete_media([media_id])
        self._selected_media_ids.discard(media_id)
        self.refresh(force=True)

    def add_tag(self, tag: str) -> None:
        tags = publish_config_service.parse_tags(self.tags_input.toPlainText())
        if tag not in tags:
            tags.append(tag)
        self.tags_input.setPlainText(" ".join(f"#{item}" for item in tags))

    def delete_selected_tag(self) -> None:
        item = self.tag_history.currentItem()
        if not item:
            QMessageBox.information(self, "历史话题", "请先选择要删除的话题。")
            return
        tag = item.text()
        if QMessageBox.question(self, "删除历史话题", f"确定删除 #{tag}？") != QMessageBox.StandardButton.Yes:
            return
        self.delete_history_tag(tag)

    def open_timer(self) -> None:
        dialog = TimerDialog(self, self.timer_values)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.timer_values = dialog.values()
            self._update_timer_status()

    def _common_schedule_toggled(self, checked: bool) -> None:
        self.common_schedule_date.setEnabled(checked)
        self.common_schedule_time.setEnabled(checked)
        self._sync_common_schedule_values()

    def _sync_common_schedule_values(self, *_args) -> None:
        date_text = self.common_schedule_date.date().toString("yyyy-MM-dd")
        time_text = self.common_schedule_time.time().toString("HH:mm")
        self.timer_values = {
            "enableTimer": self.common_schedule_enabled.isChecked(),
            "scheduleTime": f"{date_text} {time_text}",
            "videosPerDay": 1,
            "dailyTimes": [time_text],
            "startDays": 0,
            "timeJitterMinutes": 0,
        }

    def _update_timer_status(self) -> None:
        schedule_time = str(self.timer_values.get("scheduleTime") or "")
        date_part, _, time_part = schedule_time.replace("T", " ").partition(" ")
        parsed_date = QDate.fromString(date_part, "yyyy-MM-dd")
        parsed_time = QTime.fromString(time_part[:5], "HH:mm")

        controls = (
            self.common_schedule_enabled,
            self.common_schedule_date,
            self.common_schedule_time,
        )
        for control in controls:
            control.blockSignals(True)
        if parsed_date.isValid():
            self.common_schedule_date.setDate(parsed_date)
        if parsed_time.isValid():
            self.common_schedule_time.setTime(parsed_time)
        enabled = bool(self.timer_values.get("enableTimer"))
        self.common_schedule_enabled.setChecked(enabled)
        self.common_schedule_date.setEnabled(enabled)
        self.common_schedule_time.setEnabled(enabled)
        for control in controls:
            control.blockSignals(False)
        self._sync_common_schedule_values()

    @staticmethod
    def _validated_schedule_time(value: str | None, label: str) -> str:
        schedule_time = str(value or "").strip()
        if not schedule_time:
            raise ValueError(f"请设置{label}的发布日期和时间")
        try:
            parsed = datetime.fromisoformat(schedule_time.replace("T", " "))
        except ValueError as exc:
            raise ValueError(f"{label}的发布时间格式不正确") from exc
        if parsed <= datetime.now():
            raise ValueError(f"{label}的发布时间必须晚于当前时间")
        return parsed.strftime("%Y-%m-%d %H:%M")

    def _platform_schedule_time(self, platform_type: int) -> str:
        date_text = self.platform_schedule_dates[platform_type].date().toString("yyyy-MM-dd")
        time_text = self.platform_schedule_times[platform_type].time().toString("HH:mm")
        return f"{date_text} {time_text}"

    def _platform_collection_name(self, platform_type: int) -> str:
        combo = self.platform_collections[platform_type]
        text = combo.currentText().strip()
        return "" if text == "不选择合集" else text

    def _platform_cover_paths(self, platform_type: int) -> dict[str, str]:
        cover_paths = {
            "3:4": str(self.cover_34.currentData() or ""),
            "4:3": str(self.cover_43.currentData() or ""),
        }
        portrait_override = self.platform_cover_34[platform_type].currentData()
        landscape_override = self.platform_cover_43[platform_type].currentData()
        if portrait_override:
            cover_paths["3:4"] = str(portrait_override)
        if landscape_override:
            cover_paths["4:3"] = str(landscape_override)
        return {
            ratio: path
            for ratio, path in cover_paths.items()
            if path
        }

    @staticmethod
    def _preferred_cover_path(
        platform_type: int,
        cover_paths: dict[str, str],
    ) -> str:
        portrait_first = platform_type in {1, 3, 6, 8, 9}
        ratios = ("3:4", "4:3") if portrait_first else ("4:3", "3:4")
        return next(
            (cover_paths[ratio] for ratio in ratios if cover_paths.get(ratio)),
            "",
        )

    def _sync_publish_mode(self, checked: bool) -> None:
        self.start_btn.setText("开始预检" if checked else "开始发布")
        self.run_mode_label.setText("预发布检查" if checked else "正式发布")
        self.run_mode_label.setProperty("role", "countBadge" if checked else "warning")
        self.run_mode_label.style().unpolish(self.run_mode_label)
        self.run_mode_label.style().polish(self.run_mode_label)

    def _content_type_changed(self, *_args) -> None:
        """切换三套发布适配界面，并由一键发本地能力层过滤不支持目标。"""

        self.content_type = str(self.content_type_combo.currentData() or "video")
        hints = {
            "video": "视频适配：公众号不支持视频发布，已在目标列表中自动隐藏。",
            "article": "图文适配：六个平台均可用；B站与公众号会走文章发布通道。",
            "text": "文字适配：仅抖音、B站、公众号可用；三个平台都需要封面。",
        }
        if not hasattr(self, "media_title"):
            return
        self.change_content_type_btn.setToolTip(hints[self.content_type])
        self._selected_account_ids.intersection_update(
            {
                int(row.get("id") or 0)
                for row in self._account_rows
                if int(row.get("type") or 0) in self._available_platform_types()
            }
        )
        self._sync_content_type_interface()
        self.refresh_accounts()
        self.refresh_media()
        self.update_selected_labels()

    def _sync_content_type_interface(self) -> None:
        """调整素材区和文案说明，避免把视频字段误用于图文或纯文字。"""

        definitions = {
            "video": {
                "title": "视频素材",
                "search": "搜索视频素材名称",
                "empty": "暂无视频素材，请先在素材管理中导入。",
                "cover": "通用封面",
                "clear": "清空通用内容和全部平台适配项，不影响已选账号、视频素材和执行日志",
            },
            "article": {
                "title": "图文素材（图片序列）",
                "search": "搜索图片素材名称",
                "empty": "暂无图片素材，请先在素材管理中导入图片序列。",
                "cover": "图文封面",
                "clear": "清空通用内容和全部平台适配项，不影响已选账号、图片序列和执行日志",
            },
            "text": {
                "title": "文字内容",
                "search": "",
                "empty": "",
                "cover": "平台封面（抖音/B站/公众号必填）",
                "clear": "清空通用内容和全部平台适配项，不影响已选账号和执行日志",
            },
        }
        current = definitions[self.content_type]
        self.media_title.setText(current["title"])
        self.media_search.setPlaceholderText(current["search"])
        self.media_search.setVisible(self.content_type != "text")
        self.media_actions_widget.setVisible(self.content_type != "text")
        self.media_list.setVisible(self.content_type != "text")
        self.text_material_hint.setVisible(self.content_type == "text")
        self.media_list.setToolTip(current["empty"])
        self.clear_content_btn.setToolTip(current["clear"])
        self.cover_section_title.setText(current["cover"])
        self.common_title_input.setPlaceholderText(
            "各平台标题留空时使用这里的标题"
            if self.content_type != "text"
            else "纯文字发布使用的默认标题"
        )
        self.title_input.setPlaceholderText(
            "各平台文案留空时使用这里的正文"
            if self.content_type != "article"
            else "图文发布使用的默认正文说明"
        )

    def _select_content_type(self, index: int) -> None:
        """从三种发布入口进入对应的发布适配界面状态。"""

        previous_type = self.content_type
        self.content_type_combo.setCurrentIndex(index)
        # Qt 的可勾选按钮会先更新自身视觉状态；这里显式补一次同步，
        # 保证按钮、素材区和平台适配表单始终代表同一种发布类型。
        if self.content_type == previous_type:
            self._content_type_changed()
        for current_index, type_button in enumerate(self.content_type_buttons):
            type_button.setChecked(current_index == index)

    def collect_payloads(self, runtime_mode: str | None = None) -> list[dict]:
        self._sync_common_schedule_values()
        runtime_mode = runtime_mode or (
            "preflight" if self.preflight.isChecked() else "publish"
        )
        if runtime_mode not in {"preflight", "draft", "publish"}:
            raise ValueError("执行模式不正确")
        accounts = self.selected_accounts()
        media = self.selected_media()
        if not accounts:
            raise ValueError("请至少选择一个账号")
        if self.content_type != "text" and not media:
            material_label = "图片素材" if self.content_type == "article" else "视频素材"
            raise ValueError(f"请至少选择一个{material_label}")
        common_tags = publish_config_service.parse_tags(self.tags_input.toPlainText())
        publish_config_service.save_tags(common_tags)
        file_list = [item["file_path"] for item in media]
        payloads = []
        common_title = self.common_title_input.text().strip()
        common_description = self.title_input.toPlainText().strip()
        common_schedule_time = ""
        if self.timer_values.get("enableTimer"):
            common_schedule_time = self._validated_schedule_time(
                self.timer_values.get("scheduleTime"),
                "通用设置",
            )
        for platform_type in account_service.PLATFORM_ORDER:
            if platform_type not in {account["type"] for account in accounts}:
                continue
            selected = [account for account in accounts if account["type"] == platform_type]
            description = self.platform_texts[platform_type].toPlainText().strip() or common_description
            title = self.platform_titles[platform_type].text().strip() or common_title
            if not title:
                title = next((line.strip() for line in description.splitlines() if line.strip()), description)
            tags = (
                publish_config_service.parse_tags(
                    self.platform_tags[platform_type].toPlainText()
                )
                or common_tags
            )
            platform_name = account_service.PLATFORMS[platform_type]
            has_platform_schedule = self.platform_schedule_enabled[platform_type].isChecked()
            cover_paths = self._platform_cover_paths(platform_type)
            schedule_time = (
                self._validated_schedule_time(
                    self._platform_schedule_time(platform_type),
                    platform_name,
                )
                if has_platform_schedule
                else common_schedule_time
            )
            if runtime_mode == "draft":
                schedule_time = ""
            preferred_cover = self._preferred_cover_path(platform_type, cover_paths)
            capability_check = oneclick_capabilities.validate_payload(
                {
                    "platform": platform_name,
                    "content_type": self.content_type,
                    "title": title,
                    "description": description,
                    "fileList": file_list,
                    "coverPath": preferred_cover,
                }
            )
            if not capability_check["valid"]:
                raise ValueError("\n".join(capability_check["errors"]))
            payload = {
                "contentType": self.content_type,
                "type": platform_type,
                "title": title,
                "description": description,
                "tags": tags,
                "fileList": file_list,
                "accountList": [account["filePath"] for account in selected],
                "accountIds": [int(account["id"]) for account in selected],
                "accountDisplayNames": [
                    str(account.get("userName") or account.get("profileName") or "")
                    for account in selected
                ],
                "coverPath": preferred_cover,
                "coverPaths": cover_paths,
                "oneclickCapability": capability_check,
                "runtimeMode": runtime_mode,
                "debugDryRun": runtime_mode == "preflight",
                "saveDraftOnly": runtime_mode == "draft",
                "debugDryRunHoldBrowser": runtime_mode == "preflight" and not self.background_mode.isChecked(),
                "backgroundMode": self.background_mode.isChecked(),
                "originalDeclaration": self.original_declaration.isChecked(),
                "aiGenerated": self.ai_generated_content.isChecked(),
                "aiDeclarationExplicitlyConfirmed": (
                    self._ai_declaration_explicitly_confirmed
                ),
                "visibility": self.common_visibility.currentData(),
                "collectionName": self._platform_collection_name(platform_type),
                "enableTimer": bool(schedule_time),
                "scheduleTime": schedule_time or None,
                "scheduleTimezone": wechat_publish_policy.local_timezone_name(),
                "videosPerDay": 1,
                "dailyTimes": [schedule_time[-5:]] if schedule_time else [],
                "startDays": 0,
                "timeJitterMinutes": 0,
            }
            if platform_type == 10 and self.content_type == "article":
                payload["imagePlacements"] = [
                    {
                        **self._imported_article_image_specs.get(
                            int(item.get("id") or 0),
                            {},
                        ),
                        "path": str(item.get("file_path") or ""),
                    }
                    for item in media
                    if item.get("file_path")
                ]
            if platform_type in {1, 10}:
                payload["aiDisclosure"] = dict(self._imported_ai_disclosure)
            if platform_type == 10:
                if self._imported_wechat_article_template:
                    payload["wechatArticleTemplate"] = (
                        self._imported_wechat_article_template
                    )
                payload["wechatGroupNotification"] = bool(
                    self.wechat_group_notification
                    and self.wechat_group_notification.isChecked()
                )
            if platform_type == 5:
                payload.update(
                    {
                        "biliTitle": title,
                        "biliPartition": self.bili_partition.currentText(),
                        "biliType": self.bili_type.currentText(),
                        "biliDesc": description,
                    }
                )
            if platform_type == 7:
                payload["madeForKids"] = bool(
                    self.youtube_made_for_kids
                    and self.youtube_made_for_kids.isChecked()
                )
                payload["notifySubscribers"] = bool(
                    self.youtube_notify_subscribers is None
                    or self.youtube_notify_subscribers.isChecked()
                )
            if platform_type == 8:
                payload["shareToFeed"] = bool(
                    self.instagram_share_to_feed is None
                    or self.instagram_share_to_feed.isChecked()
                )
            if platform_type == 3:
                payload["syncToToutiao"] = bool(
                    self.douyin_sync_toutiao
                    and self.douyin_sync_toutiao.isChecked()
                )
                location_text = (
                    self.douyin_location_keyword.text().strip()
                    if self.douyin_location_keyword is not None
                    else ""
                )
                location = douyin_location_service.normalize_location_candidate(
                    self._douyin_selected_location
                )
                if location_text:
                    if not location or location["name"] != " ".join(location_text.split()):
                        raise ValueError(
                            "请从抖音官方地点搜索结果中选择发布定位，不能只填写关键词"
                        )
                    source_account_id = int(
                        self._douyin_selected_location.get("sourceAccountId") or 0
                    )
                    selected_account_ids = {
                        int(account.get("id") or 0) for account in selected
                    }
                    if source_account_id and source_account_id not in selected_account_ids:
                        raise ValueError("抖音账号已变更，请重新搜索并选择发布定位")
                    payload["locationKeyword"] = location["name"]
                    payload["locationPoi"] = location
                else:
                    payload["locationKeyword"] = ""
                    payload["locationPoi"] = {}
            if platform_type in self.platform_categories:
                payload["category"] = self.platform_categories[platform_type].currentData() or None
            if platform_type in self.platform_visibility:
                platform_visibility = self.platform_visibility[platform_type].currentData()
                if platform_visibility:
                    payload["visibility"] = platform_visibility
            payloads.append(payload)
        return payloads

    def create_task(self) -> None:
        if self.active_task_id and self.task_timer.isActive():
            QMessageBox.information(self, "发布中心", "当前发布任务正在执行，请等待完成后再启动新任务。")
            return
        try:
            payloads = self.collect_payloads()
            runtime_mode = (
                "preflight" if self.preflight.isChecked() else "publish"
            )
            summary = self.build_publish_summary(payloads, runtime_mode)
        except Exception as exc:
            QMessageBox.warning(self, "发布中心", str(exc))
            return
        if PublishConfirmDialog(summary, self).exec() != QDialog.DialogCode.Accepted:
            self.task_status_label.setText("发布任务：已返回修改")
            return
        meta_browser_payloads = [
            payload
            for payload in payloads
            if int(payload.get("type") or 0) in {8, 9}
        ]
        if runtime_mode == "publish" and meta_browser_payloads:
            if not self.confirm_meta_browser_publish(meta_browser_payloads):
                self.task_status_label.setText("Meta 发布任务：已返回修改")
                return
        try:
            task = publish_service.start_desktop_publish(payloads)
        except Exception as exc:
            QMessageBox.warning(self, "发布中心", str(exc))
            return
        self.active_task_id = int(task["id"])
        self.active_task_is_preflight = self.preflight.isChecked()
        self.active_task_mode = runtime_mode
        self.active_task_background_mode = all(
            bool(payload.get("backgroundMode", True)) for payload in payloads
        )
        self.active_task_started_at = datetime.now()
        self.seen_event_ids.clear()
        self.log.clear()
        mode_text = "预发布检查" if self.active_task_is_preflight else "正式发布"
        self.log.append(f"已创建{mode_text}任务：{task['taskNo']}，共 {task['itemCount']} 个执行项。")
        browser_mode_text = "后台" if self.active_task_background_mode else "前台"
        self.log.append(f"浏览器模式：{browser_mode_text}。正在准备账号检查和上传流程，后续进度会在这里更新。")
        self._update_task_progress({"status": "pending", "dryRun": 1 if self.active_task_is_preflight else 0, "itemCount": task.get("itemCount", 0)})
        self._set_running(True, f"{mode_text}运行中：{task['taskNo']}")
        self.task_timer.start()
        if self.preflight.isChecked():
            overseas_payloads = [
                payload
                for payload in payloads
                if int(payload.get("type", 0))
                in account_service.OVERSEAS_PLATFORM_TYPES
            ]
            contains_locked_overseas = any(
                int(payload.get("type", 0)) in {6, 7}
                for payload in overseas_payloads
            )
            contains_meta_browser = any(
                int(payload.get("type", 0)) in {8, 9}
                for payload in overseas_payloads
            )
            if self.active_task_background_mode:
                message = "任务已开始。上传和表单检查将在无窗口后台完成，预检结束后自动关闭会话。"
            else:
                message = "任务已开始。程序会显示发布页面并停在最终发布前，检查完成后请关闭自动化浏览器。"
            if contains_locked_overseas:
                message += "任务包含仍保持正式发布锁定的海外目标。"
            elif contains_meta_browser:
                message += "完成后可选择 Meta 可见浏览器确认式发布。"
            else:
                message += "完成后会再次询问是否启动正式发布。"
            QMessageBox.information(self, "预发布检查", message)
        else:
            message = (
                "正式发布已开始。流程会在无窗口后台执行，你可以在执行日志和进度条查看结果。"
                if self.active_task_background_mode
                else "正式发布已开始。自动化浏览器会显示在前台。"
            )
            QMessageBox.information(self, "发布中心", message)

    def create_draft_task(self) -> None:
        if self.active_task_id and self.task_timer.isActive():
            QMessageBox.information(
                self,
                "发布中心",
                "当前发布任务正在执行，请等待完成后再保存平台草稿。",
            )
            return
        selected_account_rows = self.selected_accounts()
        selected_types = {
            int(account.get("type", 0) or 0)
            for account in selected_account_rows
        }
        unsupported_draft_platforms = [
            account_service.DRAFT_UNSUPPORTED_PLATFORM_MESSAGES[platform_type]
            for platform_type in account_service.PLATFORM_ORDER
            if platform_type in selected_types
            and platform_type
            in account_service.DRAFT_UNSUPPORTED_PLATFORM_MESSAGES
        ]
        if unsupported_draft_platforms:
            platform_text = "\n".join(
                f"- {item}" for item in unsupported_draft_platforms
            )
            QMessageBox.warning(
                self,
                "所选平台不支持保存草稿",
                f"{platform_text}\n\n"
                "目前只有视频号和B站可以保存并回读平台草稿。"
                "为避免误报成功，桌面端不会为上述平台创建草稿任务。\n\n"
                "请取消选择上述平台后再保存草稿；上述平台请使用前台"
                "“预发布检查”，确认页面内容和定时时间后再人工发布。",
            )
            self.task_status_label.setText(
                "平台草稿：目前仅视频号和B站支持"
            )
            return
        if 2 in selected_types:
            timer_source = ""
            if self.platform_schedule_enabled[2].isChecked():
                timer_source = "视频号单独定时发布"
            elif self.timer_values.get("enableTimer"):
                timer_source = "通用定时发布"
            if timer_source:
                QMessageBox.warning(
                    self,
                    "视频号无法保存定时草稿",
                    f"当前已开启{timer_source}。\n"
                    "视频号平台规定：设置定时发布后无法保存草稿。\n\n"
                    "请取消视频号定时发布后再点击“保存平台草稿”；"
                    "如需保留定时设置，请改用预发布检查或正式发布。",
                )
                return
            if (
                self.ai_generated_content.isChecked()
                and not self.confirm_tencent_ai_draft_limitation()
            ):
                self.task_status_label.setText("平台草稿：已返回修改")
                return
        if 5 in selected_types:
            timer_source = ""
            if self.platform_schedule_enabled[5].isChecked():
                timer_source = "B站单独定时发布"
            elif self.timer_values.get("enableTimer"):
                timer_source = "通用定时发布"
            if timer_source:
                QMessageBox.warning(
                    self,
                    "B站草稿不会保留定时",
                    f"当前已开启{timer_source}。\n"
                    "B站草稿箱仅保存部分字段，重新打开草稿后定时发布"
                    "会恢复为关闭状态。\n\n"
                    "请取消B站定时发布后再点击“保存平台草稿”；"
                    "如需核对或保留定时时间，请改用预发布检查或正式发布。",
                )
                self.task_status_label.setText("平台草稿：已返回修改")
                return
            partition = self.bili_partition.currentText().strip()
            if (
                partition
                and not self.confirm_bilibili_partition_draft_limitation(
                    partition
                )
            ):
                self.task_status_label.setText("平台草稿：已返回修改")
                return
        try:
            payloads = self.collect_payloads("draft")
            unsupported_overseas = [
                account_service.PLATFORMS.get(
                    int(payload.get("type", 0)),
                    "海外平台",
                )
                for payload in payloads
                if int(payload.get("type", 0))
                in account_service.OVERSEAS_PLATFORM_TYPES
            ]
            if unsupported_overseas:
                raise ValueError(
                    "以下海外目标没有可验证的浏览器草稿通道："
                    + "、".join(unsupported_overseas)
                )
            summary = self.build_publish_summary(payloads, "draft")
        except Exception as exc:
            QMessageBox.warning(self, "保存平台草稿", str(exc))
            return

        if PublishConfirmDialog(summary, self).exec() != QDialog.DialogCode.Accepted:
            self.task_status_label.setText("平台草稿：已返回修改")
            return
        try:
            task = publish_service.start_desktop_publish(payloads)
        except Exception as exc:
            QMessageBox.warning(self, "保存平台草稿", str(exc))
            return

        self.active_task_id = int(task["id"])
        self.active_task_is_preflight = False
        self.active_task_mode = "draft"
        self.active_task_background_mode = self.background_mode.isChecked()
        self.active_task_started_at = datetime.now()
        self.seen_event_ids.clear()
        self.log.clear()
        self.log.append(
            f"已创建平台草稿任务：{task['taskNo']}，"
            f"共 {task['itemCount']} 个执行项。"
        )
        self.log.append(
            "草稿模式只允许平台草稿动作，代码层不会定位或点击最终发布按钮。"
        )
        self._update_task_progress(
            {
                "status": "pending",
                "mode": "desktop_draft",
                "dryRun": 0,
                "itemCount": task.get("itemCount", 0),
            }
        )
        self._set_running(True, f"平台草稿保存中：{task['taskNo']}")
        self.task_timer.start()
        QMessageBox.information(
            self,
            "保存平台草稿",
            (
                "任务已开始。程序会在无窗口后台上传视频、填写数据并保存到各平台草稿。"
                if self.active_task_background_mode
                else "任务已开始。平台页面会显示在前台，程序只执行草稿保存。"
            ),
        )

    def confirm_tencent_ai_draft_limitation(self) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle("视频号 AI 声明提示")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText("视频号草稿不会保存“含 AI 生成内容”声明")
        box.setInformativeText(
            "这是视频号平台当前的草稿限制：编辑页可以选择该声明，"
            "但保存草稿后再次打开时会恢复为未选择。\n\n"
            "继续保存只会保留视频和其他填写内容。正式发布前请打开视频号草稿，"
            "重新选择“含 AI 生成内容”并确认无误。"
        )
        continue_btn = box.addButton(
            "继续保存草稿",
            QMessageBox.ButtonRole.AcceptRole,
        )
        return_btn = box.addButton(
            "返回修改",
            QMessageBox.ButtonRole.RejectRole,
        )
        box.setDefaultButton(return_btn)
        box.exec()
        return box.clickedButton() == continue_btn

    def confirm_bilibili_partition_draft_limitation(
        self,
        partition: str,
    ) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle("B站草稿分区提示")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(
            f"B站草稿重新打开后，“{partition}”会回切为“影视”"
        )
        box.setInformativeText(
            "这是B站平台草稿接口的限制：草稿可以保存视频、标题、"
            "文案等内容，但不会保存新版分区值。\n\n"
            f"继续保存后，正式发布前必须重新选择“{partition}”。"
            "桌面端正式发布流程会自动重新选择并校验；"
            "从B站草稿箱人工发布时需要手动重选。"
        )
        continue_btn = box.addButton(
            "仍然保存草稿",
            QMessageBox.ButtonRole.AcceptRole,
        )
        return_btn = box.addButton(
            "返回修改",
            QMessageBox.ButtonRole.RejectRole,
        )
        box.setDefaultButton(return_btn)
        box.exec()
        return box.clickedButton() == continue_btn

    def import_release_bundle(self) -> None:
        if self.active_task_id and self.task_timer.isActive():
            QMessageBox.information(self, "导入发布包", "当前发布任务正在执行，请等待完成后再导入。")
            return
        default_dir = (
            Path.home()
            / "Documents/AI/codex/Projects/内容策略/墨白/outputs/发布准备"
        )
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择墨白本地浏览器发布包",
            str(default_dir if default_dir.exists() else Path.home()),
            "JSON 发布包 (*.json)",
        )
        if not path:
            return
        try:
            result = mobai_release_importer.stage_mobai_browser_bundle(Path(path).resolve())
        except Exception as exc:
            QMessageBox.warning(self, "导入发布包", str(exc))
            return

        self.refresh()
        missing = result["missing_platforms"]
        if missing:
            QMessageBox.information(
                self,
                "发布包已导入",
                f"素材已安全导入 {result['imported_count']} 个。\n\n"
                f"以下平台还没有可用的墨白账号：{'、'.join(missing)}\n"
                "请先到账号管理完成登录，再重新导入该发布包。",
            )
            return

        payloads = result["payloads"]
        for payload in payloads:
            payload["backgroundMode"] = self.background_mode.isChecked()
            payload["debugDryRunHoldBrowser"] = not self.background_mode.isChecked()
        platform_names = [account_service.PLATFORMS[int(item["type"])] for item in payloads]
        browser_mode_text = "无窗口后台运行，预检后自动关闭" if self.background_mode.isChecked() else "前台显示，关闭窗口后结束预检"
        summary = (
            "墨白发布包预发布检查\n\n"
            f"平台任务：{len(payloads)} 个\n"
            f"覆盖平台：{'、'.join(dict.fromkeys(platform_names))}\n"
            f"浏览器模式：{browser_mode_text}\n"
            "执行模式：只填写上传页面，不点击最终发布\n"
            "海外正式发布：锁定"
        )
        if PublishConfirmDialog(summary, self).exec() != QDialog.DialogCode.Accepted:
            return
        try:
            task = publish_service.start_desktop_publish(payloads)
        except Exception as exc:
            QMessageBox.warning(self, "导入发布包", f"启动预发布检查失败：{exc}")
            return

        self.active_task_id = int(task["id"])
        self.active_task_is_preflight = True
        self.active_task_mode = "preflight"
        self.active_task_background_mode = self.background_mode.isChecked()
        self.active_task_started_at = datetime.now()
        self.seen_event_ids.clear()
        self.log.clear()
        self.log.append(f"已导入墨白发布包并启动预发布任务：{task['taskNo']}")
        self.log.append(f"浏览器模式：{'后台' if self.active_task_background_mode else '前台'}；所有平台都不会点击最终发布。")
        self._update_task_progress({"status": "pending", "dryRun": 1, "itemCount": task.get("itemCount", 0)})
        self._set_running(True, f"发布包预检运行中：{task['taskNo']}")
        self.task_timer.start()
        QMessageBox.information(
            self,
            "发布包预检",
            (
                "任务已开始。程序会在无窗口后台依次检查账号、上传素材并核对表单，完成后自动关闭会话。"
                if self.active_task_background_mode
                else "任务已开始。程序会依次显示各平台发布页面并停在最终发布前。"
            ),
        )

    def import_wechat_content_bundle(self) -> None:
        """安全带入公众号文字内容包，始终停留在编辑和预检阶段。"""

        if self.active_task_id and self.task_timer.isActive():
            QMessageBox.information(self, "导入公众号内容包", "当前发布任务正在执行，请等待完成后再导入。")
            return
        picker = WechatContentBundlePickerDialog(self)
        if picker.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            bundle = wechat_content_bundle.load_wechat_content_bundle(picker.manifest_path)
            imported = media_service.import_files_with_records(
                [bundle["coverPath"]],
                category="公众号内容包",
            )
            if len(imported) != 1:
                raise ValueError("封面导入失败，请确认内容包中的封面图片可读取")
        except Exception as exc:
            QMessageBox.warning(self, "导入公众号内容包", str(exc))
            return

        # 内容包只适配公众号文字发布：清空原素材和目标，避免混入其它平台任务。
        self._select_content_type(2)
        self.preflight.setChecked(True)
        self._selected_media_ids.clear()
        self._imported_wechat_article_template = ""
        self._selected_account_ids = {
            int(row["id"])
            for row in account_service.list_accounts()
            if int(row.get("type") or 0) == 10
        }
        self.common_title_input.setText(bundle["title"])
        self.title_input.setPlainText(bundle["body"])
        if 10 in self.platform_titles:
            self.platform_titles[10].setText(bundle["title"])
        if 10 in self.platform_texts:
            self.platform_texts[10].setPlainText(bundle["body"])

        cover_stored_name = str(imported[0]["file_path"])
        self.refresh(force=True)
        self._set_combo_data(self.cover_34, cover_stored_name)
        self._set_combo_data(self.cover_43, cover_stored_name)
        self._set_combo_data(self.platform_cover_34[10], cover_stored_name)
        self._set_combo_data(self.platform_cover_43[10], cover_stored_name)
        self._refresh_platform_cover_previews()
        self.update_cover_summary()

        selected_cover = self._preferred_cover_path(10, self._platform_cover_paths(10))
        if not selected_cover:
            QMessageBox.warning(
                self,
                "内容已导入，封面待选择",
                "标题和正文已带入，封面也已导入素材库；但它不符合当前客户端的 3:4 或 4:3 封面比例。"
                "请换一张 3:4 或 4:3 封面后再执行预发布检查。",
            )
            return

        account_count = len(self._selected_account_ids)
        account_hint = (
            f"已选择 {account_count} 个公众号账号。"
            if account_count
            else "尚未发现公众号账号，请先到账号管理完成登录后再预检。"
        )
        QMessageBox.information(
            self,
            "公众号内容包已导入",
            "已带入标题、正文与封面，并强制切换为“预发布检查”。\n\n"
            f"{account_hint}\n"
            "导入本身不会上传、保存草稿或发表；请核对内容后再点击“开始预检”。",
        )

    def import_content_bundle(self, expected_type: str) -> None:
        """导入统一内容包，并只把平台差异作为可选覆盖字段带入。"""

        if self.active_task_id and self.task_timer.isActive():
            QMessageBox.information(self, "导入内容包", "当前发布任务正在执行，请等待完成后再导入。")
            return
        labels = {"video": "视频包", "article": "图文包", "text": "文字包"}
        indexes = {"video": 0, "article": 1, "text": 2}
        picker = WechatContentBundlePickerDialog(self, labels[expected_type])
        if picker.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            bundle = content_bundle.load_content_bundle(
                picker.manifest_path,
                expected_type=expected_type,
            )
            publish_schedule = dict(bundle.get("publishSchedule") or {})
            if publish_schedule.get("enabled"):
                local_timezone = wechat_publish_policy.local_timezone_name()
                if publish_schedule.get("timezone") != local_timezone:
                    raise ValueError(
                        "内容包定时时区与当前客户端不一致："
                        f"内容包={publish_schedule.get('timezone')}，"
                        f"客户端={local_timezone}"
                    )
            source_paths = list(dict.fromkeys([
                *bundle["assetPaths"],
                *bundle["coverPaths"].values(),
            ]))
            imported = media_service.import_files_with_records(source_paths, category="内容包")
            if len(imported) != len(source_paths):
                raise ValueError("部分素材导入失败，请确认内容包内的素材均可读取")
        except Exception as exc:
            QMessageBox.warning(self, "导入内容包", str(exc))
            return

        by_source = {str(row["sourcePath"]): row for row in imported}
        self._imported_article_image_specs.clear()
        self._imported_ai_disclosure = dict(bundle.get("aiDisclosure") or {})
        self._imported_wechat_article_template = str(
            bundle.get("wechatArticleTemplate") or ""
        )
        self.ai_generated_content.setChecked(
            bool(
                self._imported_ai_disclosure.get("containsAiGeneratedContent")
                and self._imported_ai_disclosure.get(
                    "allowPlatformAutoDeclaration"
                )
            )
        )
        # 内容包自动带入的勾选状态不能冒充用户亲自确认；toggled 同时兼容
        # 鼠标、键盘和辅助功能操作，因此必须在程序化赋值之后重置授权位。
        self._ai_declaration_explicitly_confirmed = False
        self.original_declaration.setChecked(
            bool(bundle.get("originalDeclaration", False))
        )
        publish_schedule = dict(bundle.get("publishSchedule") or {})
        if publish_schedule.get("enabled"):
            date_text, time_text = str(publish_schedule["localTime"]).split(" ", 1)
            self.timer_values = {
                "enableTimer": True,
                "scheduleTime": publish_schedule["localTime"],
                "videosPerDay": 1,
                "dailyTimes": [time_text],
                "startDays": 0,
                "timeJitterMinutes": 0,
            }
            self._update_timer_status()
        self._select_content_type(indexes[expected_type])
        self.preflight.setChecked(True)
        self._selected_media_ids = {
            int(by_source[path]["id"])
            for path in bundle["assetPaths"]
        }
        if expected_type == "article":
            self._imported_article_image_specs = {
                int(by_source[item["path"]]["id"]): {
                    "placement": str(item.get("placement") or ""),
                    "anchor": str(item.get("anchor") or ""),
                    "explicit": bool(item.get("explicit")),
                }
                for item in bundle["articleImages"]
            }
        preferred_platforms = set(bundle["preferredPlatforms"])
        self._selected_account_ids = {
            int(row["id"])
            for row in account_service.list_accounts()
            if row.get("platformName") in preferred_platforms
            and oneclick_capabilities.supports(str(row.get("platformName") or ""), expected_type)
        }
        self.common_title_input.setText(bundle["title"])
        self.title_input.setPlainText(bundle["body"])
        self.tags_input.setPlainText("\n".join(f"#{tag}" for tag in bundle["tags"]))
        for platform_type in self.platform_titles:
            self.platform_titles[platform_type].clear()
            self.platform_texts[platform_type].clear()
            self.platform_tags[platform_type].clear()
        type_by_platform = {
            oneclick_capabilities.canonical_platform(name): platform_type
            for platform_type, name in account_service.PLATFORMS.items()
        }
        for platform, override in bundle["platformOverrides"].items():
            platform_type = type_by_platform.get(oneclick_capabilities.canonical_platform(platform))
            if platform_type not in self.platform_titles:
                continue
            if "title" in override:
                self.platform_titles[platform_type].setText(override["title"])
            if "body" in override:
                self.platform_texts[platform_type].setPlainText(override["body"])
            if "tags" in override:
                self.platform_tags[platform_type].setPlainText(
                    "\n".join(f"#{tag}" for tag in override["tags"])
                )

        self.refresh(force=True)
        for ratio, source_path in bundle["coverPaths"].items():
            combo = self.cover_34 if ratio == "3:4" else self.cover_43
            self._set_combo_data(combo, str(by_source[source_path]["file_path"]))
        self._refresh_platform_cover_previews()
        self.update_cover_summary()
        selected_count = len(self._selected_account_ids)
        account_hint = (
            f"已按内容包偏好选择 {selected_count} 个可用账号。"
            if selected_count
            else "内容包未匹配到可用账号，请在左侧手动选择目标账号。"
        )
        ai_notice = ""
        if self._imported_ai_disclosure.get("containsAiGeneratedContent"):
            if self._imported_ai_disclosure.get(
                "allowPlatformAutoDeclaration"
            ):
                ai_notice = "\nAI 内容依据已验证，内容包明确允许自动声明。"
            else:
                ai_notice = (
                    "\n内容包标记了 AI 辅助，但未授权自动声明；"
                    "正式发布前需由你亲自勾选确认。"
                )
        template_notice = (
            "\n公众号将使用“硅基进化科技编辑版”正文模板。"
            if self._imported_wechat_article_template == "silicon-evolution-tech-v1"
            else ""
        )
        QMessageBox.information(
            self,
            f"{labels[expected_type]}已导入",
            "已带入内容、素材、封面和可选平台覆盖，并强制切换为“预发布检查”。\n\n"
            f"{account_hint}{ai_notice}{template_notice}\n"
            "导入本身不会上传、保存草稿或发表；请核对内容后再点击“开始预检”。",
        )

    def poll_task(self) -> None:
        if not self.active_task_id:
            self.task_timer.stop()
            return
        task = task_service.get_task(self.active_task_id)
        if not task:
            return
        status = task.get("status")
        self._update_task_progress(task)
        for event in task.get("events", []):
            event_id = int(event["id"])
            if event_id in self.seen_event_ids:
                continue
            self.seen_event_ids.add(event_id)
            created = event.get("createdAt") or ""
            message = event.get("message") or event.get("eventType") or ""
            level = event.get("level") or "info"
            self.log.append(f"[{created}] [{level}] {message}")
            if event.get("eventType") == "wechat_verification_required":
                self._show_wechat_verification()
        if task.get("status") not in ("pending", "running"):
            self.task_timer.stop()
            status_text = self._status_text(status)
            self.log.append(f"任务结束：{status_text}")
            self._set_running(False, f"发布任务结束：{status_text}")
            self._refresh_task_page()
            if self.active_task_mode == "preflight" and status in ("success", "partial_failed"):
                choice = self.preflight_complete_choice(task, status_text)
                if choice == "formal":
                    self.start_formal_publish_from_task(task)
                else:
                    self.log.append("预发布检查已完成，本次没有启动正式发布。")
                    self.task_status_label.setText("预发布检查完成：未启动正式发布")
                    self.active_task_id = None
            elif self.active_task_mode == "draft":
                self.active_task_id = None
                try:
                    draft_payloads = json.loads(task.get("payloadJson") or "[]")
                except (TypeError, ValueError, json.JSONDecodeError):
                    draft_payloads = []
                draft_boundary = (
                    "\nTikTok 成功项仅代表视频已进入官方收件箱，仍未公开发布；"
                    "其他成功项均已获得平台草稿证据。"
                    if any(
                        int(item.get("type") or 0) == 6
                        for item in draft_payloads
                        if isinstance(item, dict)
                    )
                    else "\n已成功的执行项均已获得平台草稿证据；"
                    "失败项不会记为已保存。"
                )
                QMessageBox.information(
                    self,
                    "平台草稿任务完成",
                    self._finish_message(task, status_text)
                    + draft_boundary,
                )
            else:
                self.active_task_id = None
                QMessageBox.information(self, "发布任务完成", self._finish_message(task, status_text))

    def _show_wechat_verification(self) -> None:
        """把后台执行器的二维码带回一键发前台，不主动显示浏览器。"""

        if not self.active_task_id:
            return
        request_id = verification_broker.request_for_task(self.active_task_id)
        if not request_id:
            self.log.append("[error] 微信验证请求不存在，发布已保持暂停")
            return
        if self._wechat_verification_dialog:
            self._wechat_verification_dialog.raise_()
            self._wechat_verification_dialog.activateWindow()
            return
        dialog = WechatVerificationDialog(request_id, self)
        self._wechat_verification_dialog = dialog
        try:
            dialog.exec()
        finally:
            self._wechat_verification_dialog = None

    def preflight_complete_choice(self, task: dict, status_text: str) -> str:
        try:
            payloads = json.loads(task.get("payloadJson") or "[]")
        except json.JSONDecodeError:
            payloads = []
        contains_overseas = any(
            int(payload.get("type", 0) or 0) in account_service.OVERSEAS_PLATFORM_TYPES
            for payload in payloads
            if isinstance(payload, dict)
        )
        overseas_payloads = [
            payload
            for payload in payloads
            if isinstance(payload, dict)
            and int(payload.get("type", 0) or 0)
            in account_service.OVERSEAS_PLATFORM_TYPES
        ]
        browser_meta = [
            payload
            for payload in overseas_payloads
            if int(payload.get("type") or 0) in {8, 9}
        ]
        meta_browser_ready = bool(overseas_payloads) and (
            len(browser_meta) == len(overseas_payloads)
        )
        formal_overseas_ready = meta_browser_ready
        box = QMessageBox(self)
        box.setWindowTitle("预发布检查完成")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText("上传链路与发布表单检查已经完成。")
        session_note = (
            "后台浏览器已经自动关闭；本次结果不是可恢复的平台草稿。"
            if self.active_task_background_mode
            else "前台检查会话已经结束；本次结果不是可恢复的平台草稿。"
        )
        if contains_overseas and not formal_overseas_ready:
            box.setInformativeText(
                f"{self._finish_message(task, status_text)}\n\n"
                "TikTok 与 YouTube 当前只开放浏览器预发布检查，"
                "正式发布保持锁定。\n"
                f"{session_note}"
            )
            manual_btn = box.addButton("我已了解", QMessageBox.ButtonRole.AcceptRole)
            box.setDefaultButton(manual_btn)
            box.exec()
            return "manual"
        action_hint = (
            "继续 Meta 确认式发布：将再显示一次独立确认，"
            "并在可见浏览器中执行。\n"
            if meta_browser_ready
            else "继续一键发布：使用同一配置重新上传并执行正式发布。\n"
        )
        box.setInformativeText(
            f"{self._finish_message(task, status_text)}\n\n"
            f"{session_note}\n\n"
            f"请选择下一步操作：\n{action_hint}"
            "暂不发布：只保留本次预检结果。"
        )
        formal_btn = box.addButton(
            "继续 Meta 确认式发布"
            if meta_browser_ready
            else "继续一键发布",
            QMessageBox.ButtonRole.AcceptRole,
        )
        manual_btn = box.addButton("暂不发布", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(formal_btn)
        box.exec()
        return "formal" if box.clickedButton() == formal_btn else "manual"

    def start_formal_publish_from_task(self, task: dict) -> None:
        try:
            payloads = json.loads(task.get("payloadJson") or "[]")
            for payload in payloads:
                payload["runtimeMode"] = "publish"
                payload["debugDryRun"] = False
                payload["debugDryRunHoldBrowser"] = False
            meta_browser_payloads = [
                payload
                for payload in payloads
                if int(payload.get("type") or 0) in {8, 9}
            ]
            if meta_browser_payloads and not self.confirm_meta_browser_publish(
                meta_browser_payloads
            ):
                self.log.append("Meta 浏览器最终发布已取消。")
                self.task_status_label.setText(
                    "预发布检查完成：未启动 Meta 最终发布"
                )
                self.active_task_id = None
                return
            new_task = publish_service.start_desktop_publish(payloads)
        except Exception as exc:
            QMessageBox.warning(self, "正式发布", f"启动正式发布失败：{exc}")
            return
        self.active_task_id = int(new_task["id"])
        self.active_task_is_preflight = False
        self.active_task_mode = "publish"
        self.active_task_background_mode = all(bool(payload.get("backgroundMode", True)) for payload in payloads)
        self.active_task_started_at = datetime.now()
        self.seen_event_ids.clear()
        self.log.append("")
        self.log.append(f"已启动正式发布任务：{new_task['taskNo']}")
        self.log.append(
            "正式发布将在无窗口后台执行，完成后会在这里显示结果。"
            if self.active_task_background_mode
            else "正式发布浏览器会显示在前台，完成后会在这里显示结果。"
        )
        self._update_task_progress({"status": "pending", "dryRun": 0, "itemCount": new_task.get("itemCount", 0)})
        self._set_running(True, f"正式发布运行中：{new_task['taskNo']}")
        self.task_timer.start()

    def confirm_meta_browser_publish(self, payloads: list[dict]) -> bool:
        """一次性写入 Meta 浏览器发布的两项独立确认。"""

        if MetaBrowserPublishConfirmDialog(self).exec() != QDialog.DialogCode.Accepted:
            return False
        for payload in payloads:
            if int(payload.get("type") or 0) not in {8, 9}:
                continue
            payload[META_BROWSER_PUBLISH_CONFIRMED] = True
            payload[META_BROWSER_AUTOMATION_ACKNOWLEDGED] = True
            payload["backgroundMode"] = False
            payload["debugDryRunHoldBrowser"] = False
        return True

    def _set_running(self, running: bool, message: str) -> None:
        self.start_btn.setEnabled(not running)
        self.save_platform_draft_btn.setEnabled(not running)
        self.refresh_btn.setEnabled(not running)
        self.task_status_label.setText(message)
        if not running:
            self.active_task_started_at = None

    def _status_text(self, status: str | None) -> str:
        return {
            "pending": "等待执行",
            "running": "执行中",
            "success": "成功",
            "partial_failed": "部分失败",
            "failed": "失败",
            "cancelled": "已取消",
        }.get(status or "", status or "未知")

    def _update_task_progress(self, task: dict) -> None:
        total = int(task.get("itemCount") or 0)
        success = int(task.get("successCount") or 0)
        failed = int(task.get("failedCount") or 0)
        skipped = int(task.get("skippedCount") or 0)
        done = min(total, success + failed + skipped) if total else 0
        status = task.get("status")
        task_mode = str(task.get("mode") or "")
        mode = (
            "平台草稿"
            if task_mode == "desktop_draft" or self.active_task_mode == "draft"
            else "预发布检查"
            if task.get("dryRun")
            else "正式发布"
        )
        elapsed = self._elapsed_text()
        if total:
            self.task_progress.setRange(0, total)
            self.task_progress.setValue(done)
            self.task_progress.setFormat(f"{mode}：{done}/{total} 完成，成功 {success}，失败 {failed}，跳过 {skipped}")
        elif status in ("pending", "running"):
            self.task_progress.setRange(0, 0)
            self.task_progress.setFormat(f"{mode}：准备中")
        else:
            self.task_progress.setRange(0, 1)
            self.task_progress.setValue(0)
            self.task_progress.setFormat("空闲")
        self.task_status_label.setText(f"{mode}：{self._status_text(status)}{elapsed}")

    def _elapsed_text(self) -> str:
        if not self.active_task_started_at:
            return ""
        seconds = max(0, int((datetime.now() - self.active_task_started_at).total_seconds()))
        minutes, second = divmod(seconds, 60)
        if minutes:
            return f"｜已用时 {minutes}分{second:02d}秒"
        return f"｜已用时 {second}秒"

    def _finish_message(self, task: dict, status_text: str) -> str:
        lines = [
            f"任务状态：{status_text}\n"
            f"执行项：共 {task.get('itemCount') or 0} 个，"
            f"成功 {task.get('successCount') or 0} 个，"
            f"失败 {task.get('failedCount') or 0} 个，"
            f"跳过 {task.get('skippedCount') or 0} 个。"
        ]
        lines.extend(self._task_item_result_lines(task, "failed", "失败平台"))
        lines.extend(self._task_item_result_lines(task, "skipped", "跳过平台"))
        lines.append("详细过程可以在任务记录里查看。")
        return "\n".join(lines)

    def _task_item_result_lines(
        self,
        task: dict,
        target_status: str,
        heading: str,
    ) -> list[str]:
        grouped: dict[str, list[str]] = {}
        for item in task.get("items") or []:
            if str(item.get("status") or "") != target_status:
                continue
            platform = str(item.get("platformName") or "未知平台")
            message = self._compact_task_item_message(item.get("message"))
            reasons = grouped.setdefault(platform, [])
            if message and message not in reasons:
                reasons.append(message)

        if not grouped:
            return []

        lines = [f"{heading}："]
        for platform, reasons in grouped.items():
            if not reasons:
                lines.append(f"- {platform}")
                continue
            extra = (
                f"（另有 {len(reasons) - 1} 个不同原因）"
                if len(reasons) > 1
                else ""
            )
            lines.append(f"- {platform}：{reasons[0]}{extra}")
        return lines

    @staticmethod
    def _compact_task_item_message(
        message: object,
        limit: int = 96,
    ) -> str:
        text = " ".join(str(message or "").split())
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "…"

    def _refresh_task_page(self) -> None:
        parent = self.window()
        tasks_page = getattr(parent, "tasks", None)
        if tasks_page and hasattr(tasks_page, "refresh"):
            tasks_page.refresh()

    def build_publish_summary(
        self,
        payloads: list[dict],
        runtime_mode: str | None = None,
    ) -> str:
        runtime_mode = runtime_mode or (
            "preflight" if self.preflight.isChecked() else "publish"
        )
        mode_labels = {
            "preflight": "预发布检查",
            "draft": "保存平台草稿",
            "publish": "正式发布",
        }
        accounts = self.selected_accounts()
        media = self.selected_media()
        lines = [
            f"执行模式：{mode_labels.get(runtime_mode, runtime_mode)}",
            f"浏览器模式：{'无窗口后台运行' if self.background_mode.isChecked() else '前台显示'}",
        ]
        if runtime_mode == "draft":
            lines.append("安全边界：只保存平台草稿，不点击最终发布按钮")
        if self.timer_values.get("enableTimer"):
            lines.append(f"通用定时发布：{self.timer_values.get('scheduleTime') or '未设置'}")
        else:
            lines.append("通用定时发布：未启用")
        lines.append(f"原创声明：{'开启' if self.original_declaration.isChecked() else '关闭'}")
        lines.append(f"AI 生成内容：{'包含' if self.ai_generated_content.isChecked() else '不包含'}")
        lines.append(f"通用可见范围：{self.common_visibility.currentText()}")

        lines.append("")
        lines.append(f"账号数量：{len(accounts)}")
        for account in accounts:
            remark = f" | {account.get('remark')}" if account.get("remark") else ""
            lines.append(
                f"- {account['profileName']} | {account['platformName']} | "
                f"{account['userName']} | 浏览器会话{remark}"
            )

        lines.append("")
        lines.append(f"素材数量：{len(media)}")
        for item in media:
            remark = f" | {item.get('remark')}" if item.get("remark") else ""
            lines.append(f"- {item['filename']}{remark}")

        lines.append("")
        lines.append("发布内容：")
        lines.append(f"- 通用标题：{self.common_title_input.text().strip() or '未填写'}")
        lines.append(f"- 通用文案：{self.title_input.toPlainText().strip() or '未填写'}")
        lines.append(f"- 通用话题：{self.tags_input.toPlainText().strip() or '未填写'}")
        for payload in payloads:
            platform_name = account_service.PLATFORMS.get(int(payload.get("type")), "未知平台")
            title = payload.get("title") or payload.get("biliTitle") or "未填写"
            description = payload.get("description") or "未填写"
            tags = " ".join(f"#{tag}" for tag in payload.get("tags") or []) or "未填写"
            lines.append(f"- {platform_name}标题：{title}")
            lines.append(f"  文案：{description} / {tags}")
            lines.append(f"  合集：{payload.get('collectionName') or '不选择'}")
            lines.append(f"  发布时间：{payload.get('scheduleTime') or '立即发布'}")
            if int(payload.get("type") or 0) in account_service.OVERSEAS_PLATFORM_TYPES:
                lines.append("  海外执行通道：一键发受控浏览器")
            visibility_labels = {"public": "公开", "private": "私密", "unlisted": "不公开"}
            lines.append(f"  谁可以看：{visibility_labels.get(payload.get('visibility'), '公开')}")
            if int(payload.get("type")) == 5:
                lines.append(f"  B站分区：{payload.get('biliPartition') or '未设置'}，类型：{payload.get('biliType') or '未设置'}")
                if runtime_mode == "draft":
                    lines.append(
                        "  B站草稿限制：平台不会保留新版分区值，"
                        "重新打开可能显示“影视”；正式发布时会重新选择并校验"
                    )
            if int(payload.get("type")) == 7:
                lines.append(
                    "  YouTube 受众："
                    + ("面向儿童" if payload.get("madeForKids") else "不面向儿童")
                )
                lines.append(
                    "  订阅者通知："
                    + ("开启" if payload.get("notifySubscribers", True) else "关闭")
                )
            if int(payload.get("type")) == 8:
                lines.append(
                    "  Instagram 同时分享到动态："
                    + ("开启" if payload.get("shareToFeed", True) else "关闭")
                )
            if int(payload.get("type")) == 3:
                lines.append(
                    "  今日头条同步："
                    + ("开启" if payload.get("syncToToutiao") else "关闭")
                )
                location = payload.get("locationPoi") or {}
                location_text = str(location.get("name") or "").strip()
                location_address = str(location.get("address") or "").strip()
                if location_text and location_address:
                    location_text = f"{location_text}（{location_address}）"
                lines.append(f"  发布定位：{location_text or '不添加'}")
            if int(payload.get("type")) == 10:
                lines.append(
                    "  群发通知："
                    + ("开启" if payload.get("wechatGroupNotification", True) else "关闭")
                )
                lines.append(
                    "  原创作者："
                    + (
                        "选择当前账号第一个可用作者并回读"
                        if payload.get("originalDeclaration")
                        else "完全跳过作者控件"
                    )
                )

        lines.append("")
        lines.append("封面设置：")
        lines.append(f"- 通用竖版封面：{self.cover_34.currentText()}")
        lines.append(f"- 通用横版封面：{self.cover_43.currentText()}")
        lines.append("- 横版统一为 4:3，竖版统一为 3:4；平台单独封面优先于通用封面。")
        return "\n".join(lines)

    def payload_for_template(self) -> dict:
        return {
            "commonTitle": self.common_title_input.text(),
            "commonText": self.title_input.toPlainText(),
            "title": self.title_input.toPlainText(),
            "tags": self.tags_input.toPlainText(),
            "platformTitles": {str(k): v.text() for k, v in self.platform_titles.items()},
            "platformTexts": {str(k): v.toPlainText() for k, v in self.platform_texts.items()},
            "platformTags": {
                str(k): v.toPlainText()
                for k, v in self.platform_tags.items()
            },
            "originalDeclaration": self.original_declaration.isChecked(),
            "aiGenerated": self.ai_generated_content.isChecked(),
            "commonVisibility": self.common_visibility.currentData(),
            "timerValues": dict(self.timer_values),
            "platformCollections": {
                str(platform_type): self._platform_collection_name(platform_type)
                for platform_type in self.platform_collections
            },
            "platformCoverPaths": {
                str(platform_type): {
                    "3:4": self.platform_cover_34[platform_type].currentData() or "",
                    "4:3": self.platform_cover_43[platform_type].currentData() or "",
                }
                for platform_type in self.platform_cover_34
            },
            "platformSchedules": {
                str(platform_type): {
                    "enabled": self.platform_schedule_enabled[platform_type].isChecked(),
                    "date": self.platform_schedule_dates[platform_type].date().toString("yyyy-MM-dd"),
                    "time": self.platform_schedule_times[platform_type].time().toString("HH:mm"),
                }
                for platform_type in self.platform_schedule_enabled
            },
            "biliPartition": self.bili_partition.currentText(),
            "biliType": self.bili_type.currentText(),
            "platformVisibility": {str(k): v.currentData() for k, v in self.platform_visibility.items()},
            "youtubeMadeForKids": bool(
                self.youtube_made_for_kids
                and self.youtube_made_for_kids.isChecked()
            ),
            "youtubeNotifySubscribers": bool(
                self.youtube_notify_subscribers is None
                or self.youtube_notify_subscribers.isChecked()
            ),
            "instagramShareToFeed": bool(
                self.instagram_share_to_feed is None
                or self.instagram_share_to_feed.isChecked()
            ),
            "douyinSyncToutiao": bool(
                self.douyin_sync_toutiao
                and self.douyin_sync_toutiao.isChecked()
            ),
            "douyinLocationKeyword": (
                self.douyin_location_keyword.text().strip()
                if self.douyin_location_keyword is not None
                else ""
            ),
            "douyinLocation": (
                douyin_location_service.normalize_location_candidate(
                    self._douyin_selected_location
                )
                or {}
            ),
            "wechatGroupNotification": bool(
                self.wechat_group_notification
                and self.wechat_group_notification.isChecked()
            ),
        }

    def save_template(self) -> None:
        name, ok = QInputDialog.getText(self, "保存模板", "模板名称：")
        if ok and name.strip():
            publish_config_service.save_template(name, self.payload_for_template())
            self.refresh_templates()

    def payload_for_saved_content(self) -> dict:
        return {
            "schemaVersion": 1,
            "content": self.payload_for_template(),
            "selectedAccountIds": sorted(self._selected_account_ids),
            "selectedAccountFiles": [
                str(row.get("filePath") or "")
                for row in self.selected_accounts()
                if row.get("filePath")
            ],
            "selectedMediaIds": sorted(self._selected_media_ids),
            "selectedMediaPaths": [
                str(row.get("file_path") or row.get("storedPath") or "")
                for row in self.selected_media()
                if row.get("file_path") or row.get("storedPath")
            ],
            "mediaCategory": self.media_category_combo.currentData(),
            "coverPaths": {
                "3:4": self.cover_34.currentData() or "",
                "4:3": self.cover_43.currentData() or "",
            },
            "preflight": self.preflight.isChecked(),
            # 后台运行属于每次进入发布中心的安全默认值，不跟随内容草稿保存。
            "backgroundMode": True,
        }

    def save_publish_content(self) -> None:
        publish_config_service.save_publish_draft(self.payload_for_saved_content())
        saved_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.content_save_status.setText(f"已保存 {saved_at}")
        self.content_save_status.setProperty("role", "success")
        self.content_save_status.style().unpolish(self.content_save_status)
        self.content_save_status.style().polish(self.content_save_status)
        QMessageBox.information(
            self,
            "保存填写内容",
            "当前账号、视频、封面和全部平台填写内容已保存在本机。"
            "\n该操作不会上传视频，也不会生成平台草稿。",
        )

    def refresh_saved_content_status(self) -> None:
        draft = publish_config_service.load_publish_draft()
        if not draft:
            self.content_save_status.setText("尚未保存")
            self.restore_content_btn.setEnabled(False)
            return
        self.content_save_status.setText(f"已保存 {draft.get('updatedAt') or ''}".strip())
        self.content_save_status.setProperty("role", "success")
        self.restore_content_btn.setEnabled(True)

    @staticmethod
    def _set_combo_data(combo: QComboBox, value: str | None) -> None:
        if not value:
            combo.setCurrentIndex(0)
            return
        index = combo.findData(value)
        if index < 0:
            target_name = Path(str(value)).name
            for candidate in range(combo.count()):
                candidate_data = combo.itemData(candidate)
                if candidate_data and Path(str(candidate_data)).name == target_name:
                    index = candidate
                    break
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _set_legacy_horizontal_cover(self, value: str | None) -> None:
        """把旧草稿的 16:9 文件尽量迁移到同篇 4:3 封面。"""

        self._set_combo_data(self.cover_43, value)
        if self.cover_43.currentData() or not value:
            return
        generic_parts = {
            "封面",
            "16x9",
            "16-9",
            "4x3",
            "4-3",
            "第一篇系列版",
            "内置4x3安全区",
            "由16x9安全裁切",
        }
        legacy_parts = {
            part
            for part in Path(str(value)).stem.replace("-", "_").split("_")
            if len(part) >= 2 and part not in generic_parts
        }
        best_index = 0
        best_score = 0
        for index in range(1, self.cover_43.count()):
            candidate = Path(str(self.cover_43.itemData(index) or "")).stem
            score = sum(
                len(part)
                for part in legacy_parts
                if part in candidate
            )
            if score > best_score:
                best_score = score
                best_index = index
        if best_score:
            self.cover_43.setCurrentIndex(best_index)

    def restore_publish_content(self, *, show_message: bool = True) -> None:
        draft = publish_config_service.load_publish_draft()
        if not draft:
            if show_message:
                QMessageBox.information(self, "恢复内容", "当前没有已保存的发布内容。")
            return
        payload = draft.get("payload") or {}
        content_payload = payload.get("content") or payload
        saved_collections = dict(content_payload.get("platformCollections") or {})
        self._apply_content_payload(content_payload)

        self.refresh_accounts()
        valid_account_ids = {int(row["id"]) for row in self._account_rows}
        saved_account_ids = {
            int(item) for item in payload.get("selectedAccountIds") or []
        }
        saved_account_files = {
            str(item) for item in payload.get("selectedAccountFiles") or []
        }
        self._selected_account_ids = saved_account_ids & valid_account_ids
        self._selected_account_ids.update(
            int(row["id"])
            for row in self._account_rows
            if str(row.get("filePath") or "") in saved_account_files
        )

        self.refresh_media()
        valid_media_ids = {int(row["id"]) for row in self._media_rows}
        saved_media_ids = {
            int(item) for item in payload.get("selectedMediaIds") or []
        }
        saved_media_paths = {
            str(item) for item in payload.get("selectedMediaPaths") or []
        }
        self._selected_media_ids = saved_media_ids & valid_media_ids
        self._selected_media_ids.update(
            int(row["id"])
            for row in self._media_rows
            if str(row.get("file_path") or row.get("storedPath") or "")
            in saved_media_paths
        )

        self.refresh_accounts()
        self.refresh_media()
        if self._douyin_selected_location:
            selected_douyin = self._platform_accounts(3)
            if len(selected_douyin) == 1:
                self._douyin_selected_location["sourceAccountId"] = int(
                    selected_douyin[0].get("id") or 0
                )
        self.refresh_covers()
        self._apply_platform_collection_values(saved_collections)
        covers = payload.get("coverPaths") or {}
        self._set_combo_data(self.cover_34, covers.get("3:4"))
        if covers.get("4:3"):
            self._set_combo_data(self.cover_43, covers.get("4:3"))
        else:
            self._set_legacy_horizontal_cover(covers.get("16:9"))
        self._apply_platform_cover_values(
            content_payload.get("platformCoverPaths") or {}
        )
        self.preflight.setChecked(bool(payload.get("preflight", True)))
        # 兼容旧草稿中的 backgroundMode 字段，但不让历史选择覆盖当前默认值。
        self.background_mode.setChecked(True)
        self.update_cover_summary()
        self.content_save_status.setText(
            f"已恢复 {draft.get('updatedAt') or ''}".strip()
        )
        if show_message:
            QMessageBox.information(self, "恢复内容", "已恢复上次保存的完整发布内容。")

    def apply_template(self) -> None:
        template_id = self.template_combo.currentData()
        if not template_id:
            return
        template = publish_config_service.get_template(int(template_id))
        if not template:
            return
        self._apply_content_payload(template["payload"])

    def _apply_content_payload(self, payload: dict) -> None:
        self.common_title_input.setText(payload.get("commonTitle", ""))
        self.title_input.setPlainText(payload.get("commonText", payload.get("title", "")))
        self.tags_input.setPlainText(payload.get("tags", ""))
        self.original_declaration.setChecked(bool(payload.get("originalDeclaration", False)))
        self.ai_generated_content.setChecked(bool(payload.get("aiGenerated", False)))
        self._ai_declaration_explicitly_confirmed = bool(
            payload.get("aiDeclarationExplicitlyConfirmed", False)
        )
        visibility_index = self.common_visibility.findData(payload.get("commonVisibility", "public"))
        self.common_visibility.setCurrentIndex(visibility_index if visibility_index >= 0 else 0)
        self.timer_values = dict(payload.get("timerValues") or {"enableTimer": False})
        self._update_timer_status()
        for key, value in (payload.get("platformTitles") or {}).items():
            if int(key) in self.platform_titles:
                self.platform_titles[int(key)].setText(value)
        for key, value in (payload.get("platformTexts") or {}).items():
            if int(key) in self.platform_texts:
                self.platform_texts[int(key)].setPlainText(value)
        for key, value in (payload.get("platformTags") or {}).items():
            if int(key) in self.platform_tags:
                self.platform_tags[int(key)].setPlainText(value)
        self._apply_platform_collection_values(
            payload.get("platformCollections") or {}
        )
        self._apply_platform_cover_values(
            payload.get("platformCoverPaths") or {}
        )
        for key, value in (payload.get("platformSchedules") or {}).items():
            platform_type = int(key)
            if platform_type not in self.platform_schedule_enabled or not isinstance(value, dict):
                continue
            enabled = bool(value.get("enabled"))
            self.platform_schedule_enabled[platform_type].setChecked(enabled)
            date_value = QDate.fromString(str(value.get("date") or ""), "yyyy-MM-dd")
            time_value = QTime.fromString(str(value.get("time") or ""), "HH:mm")
            if date_value.isValid():
                self.platform_schedule_dates[platform_type].setDate(date_value)
            if time_value.isValid():
                self.platform_schedule_times[platform_type].setTime(time_value)
        if not self.platform_titles[5].text() and payload.get("biliTitle"):
            self.platform_titles[5].setText(payload.get("biliTitle", ""))
        if not self.platform_texts[5].toPlainText() and payload.get("biliDesc"):
            self.platform_texts[5].setPlainText(payload.get("biliDesc", ""))
        self.bili_partition.setCurrentText(payload.get("biliPartition", "知识"))
        self.bili_type.setCurrentText(payload.get("biliType", "自制"))
        for key, value in (payload.get("platformCategories") or {}).items():
            combo = self.platform_categories.get(int(key))
            if combo:
                index = combo.findData(value)
                combo.setCurrentIndex(index if index >= 0 else 0)
        for key, value in (payload.get("platformVisibility") or {}).items():
            combo = self.platform_visibility.get(int(key))
            if combo:
                index = combo.findData(value)
                combo.setCurrentIndex(index if index >= 0 else 0)
        if self.youtube_made_for_kids:
            self.youtube_made_for_kids.setChecked(
                bool(payload.get("youtubeMadeForKids", False))
            )
        if self.youtube_notify_subscribers:
            self.youtube_notify_subscribers.setChecked(
                bool(payload.get("youtubeNotifySubscribers", True))
            )
        if self.instagram_share_to_feed:
            self.instagram_share_to_feed.setChecked(
                bool(payload.get("instagramShareToFeed", True))
            )
        if self.douyin_sync_toutiao:
            self.douyin_sync_toutiao.setChecked(
                bool(payload.get("douyinSyncToutiao", False))
            )
        if self.douyin_location_keyword is not None:
            location = douyin_location_service.normalize_location_candidate(
                payload.get("douyinLocation")
            )
            keyword = str(payload.get("douyinLocationKeyword") or "").strip()
            self.douyin_location_keyword.blockSignals(True)
            self.douyin_location_keyword.setText(
                location["name"] if location else keyword
            )
            self.douyin_location_keyword.blockSignals(False)
            self._douyin_selected_location = {}
            if location:
                selected_douyin = self._platform_accounts(3)
                location["sourceAccountId"] = (
                    int(selected_douyin[0].get("id") or 0)
                    if len(selected_douyin) == 1
                    else 0
                )
                self._douyin_selected_location = location
                detail = location.get("address") or "平台未返回详细地址"
                self._set_douyin_location_status(
                    f"已恢复：{location['name']}\n{detail}",
                    "success",
                )
            elif keyword:
                self._set_douyin_location_status(
                    "旧模板只保存了地点关键词，请重新搜索并选择官方地点",
                    "warning",
                )
            else:
                self._set_douyin_location_status("未添加定位")
        if self.wechat_group_notification:
            self.wechat_group_notification.setChecked(
                bool(payload.get("wechatGroupNotification", True))
            )

    def _apply_platform_cover_values(self, values: dict) -> None:
        for key, covers in values.items():
            try:
                platform_type = int(key)
            except (TypeError, ValueError):
                continue
            if (
                platform_type not in self.platform_cover_34
                or not isinstance(covers, dict)
            ):
                continue
            self._set_combo_data(
                self.platform_cover_34[platform_type],
                covers.get("3:4") or covers.get("9:16"),
            )
            self._set_combo_data(
                self.platform_cover_43[platform_type],
                covers.get("4:3") or covers.get("16:9"),
            )
            self._update_platform_cover_preview(platform_type, "3:4")
            self._update_platform_cover_preview(platform_type, "4:3")

    def delete_template(self) -> None:
        template_id = self.template_combo.currentData()
        if template_id and QMessageBox.question(self, "删除模板", "确定删除当前模板？") == QMessageBox.StandardButton.Yes:
            publish_config_service.delete_template(int(template_id))
            self.refresh_templates()
