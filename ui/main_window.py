# -*- coding: utf-8 -*-
"""桌面端主窗口。"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app_core import account_browser_service, activation_service
from app_core.branding import APP_ICON_RELATIVE_PATH, APP_TITLE, APP_VERSION, UPGRADE_STORE
from app_core.paths import AVATAR_DIR, COOKIE_DIR, DB_PATH, LOG_DIR, ROOT_DIR, VIDEO_DIR

from .background_task import BackgroundTaskRunner
from .common import button
from .account_page import AccountPage
from .dashboard_page import DashboardPage
from .help_dialog import HelpDialog
from .media_page import MediaPage
from .publish_page import PublishPage
from .task_page import TaskPage


DEFAULT_MAIN_WINDOW_SIZE = (1600, 900)
WINDOWS_MAIN_WINDOW_MAX_SIZE = (1920, 1080)
WINDOWS_MAIN_WINDOW_FILL_RATIO = 0.90


def calculate_windows_main_window_geometry(
    available_x: int,
    available_y: int,
    available_width: int,
    available_height: int,
) -> tuple[int, int, int, int]:
    """根据 Windows 可用桌面计算居中的主窗口几何。"""

    base_width, base_height = DEFAULT_MAIN_WINDOW_SIZE
    max_width, max_height = WINDOWS_MAIN_WINDOW_MAX_SIZE
    available_width = max(int(available_width), 1)
    available_height = max(int(available_height), 1)
    target_width = min(
        available_width,
        max_width,
        max(base_width, round(available_width * WINDOWS_MAIN_WINDOW_FILL_RATIO)),
    )
    target_height = min(
        available_height,
        max_height,
        max(base_height, round(available_height * WINDOWS_MAIN_WINDOW_FILL_RATIO)),
    )
    target_x = int(available_x) + max((available_width - target_width) // 2, 0)
    target_y = int(available_y) + max((available_height - target_height) // 2, 0)
    return target_x, target_y, target_width, target_height


class LicenseDialog(QDialog):
    """显示试用状态、机器码并接收设备绑定的签名激活码。"""

    def __init__(self, parent=None, *, activation_required: bool = False) -> None:
        super().__init__(parent)
        self.activation_required = activation_required
        self.setWindowTitle("激活软件" if activation_required else "授权管理")
        self.resize(660, 390)
        self.runner = BackgroundTaskRunner(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)
        heading = QLabel("激活软件" if activation_required else "授权管理")
        heading.setObjectName("dialogTitle")
        layout.addWidget(heading)

        form_panel = QFrame()
        form_panel.setProperty("subPanel", True)
        form = QFormLayout(form_panel)
        form.setContentsMargins(14, 12, 14, 12)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(8)
        self.status_label = QLabel("正在读取授权状态")
        self.status_label.setStyleSheet("color: #dc2626; font-weight: 700;")
        self.machine_label = QLabel("")
        self.machine_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.expires_label = QLabel("")
        self.trial_label = QLabel("")
        self.code_input = QLineEdit()
        self.code_input.setPlaceholderText("请输入购买后获得的激活码")
        form.addRow("激活状态", self.status_label)
        form.addRow("机器码", self.machine_label)
        form.addRow("试用到期", self.trial_label)
        form.addRow("授权有效期", self.expires_label)
        form.addRow("激活码", self.code_input)
        layout.addWidget(form_panel)
        note = QLabel(
            "软件首次打开自动开始 7 天全功能试用，试用期内不限制平台数量和发布数量。"
            f"试用到期后，请联系淘宝店铺“{UPGRADE_STORE}”，购买与当前机器码绑定的激活码。"
        )
        note.setObjectName("infoCallout")
        note.setWordWrap(True)
        layout.addWidget(note)
        actions = QHBoxLayout()
        copy_machine_btn = button("复制机器码", variant="secondary")
        copy_machine_btn.clicked.connect(self.copy_machine_code)
        refresh_btn = button("刷新授权", variant="secondary")
        refresh_btn.clicked.connect(self.refresh_license)
        activate_btn = button("激活", variant="primary")
        activate_btn.clicked.connect(self.activate_license)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setText(
            "退出软件" if activation_required else "关闭"
        )
        close.rejected.connect(self.reject)
        actions.addWidget(close)
        actions.addStretch()
        actions.addWidget(copy_machine_btn)
        actions.addWidget(refresh_btn)
        actions.addWidget(activate_btn)
        layout.addLayout(actions)
        self.refresh_license(show_message=False)

    def copy_machine_code(self) -> None:
        machine = self.machine_label.text().strip()
        if not machine:
            QMessageBox.warning(self, "复制机器码", "尚未读取到机器码，请先刷新授权。")
            return
        QApplication.clipboard().setText(machine)
        QMessageBox.information(self, "复制机器码", "机器码已复制，可以发送给卖家签发激活码。")

    def refresh_license(self, show_message: bool = True) -> None:
        status = activation_service.license_status()
        self.status_label.setText(status["statusText"])
        self.status_label.setStyleSheet(f"color: {status['color']}; font-weight: 700;")
        self.machine_label.setText(status.get("machineCode") or "")
        self.expires_label.setText(
            self._display_time(status.get("expiresAt")) if status.get("activated") else "尚未激活"
        )
        self.trial_label.setText(self._display_time(status.get("trialEndsAt")))
        if show_message:
            QMessageBox.information(self, "刷新授权", f"当前授权状态：{status['statusText']}")

    @staticmethod
    def _display_time(value: object) -> str:
        raw = str(value or "").strip()
        if not raw:
            return "未记录"
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed.astimezone().strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return raw

    def activate_license(self) -> None:
        code = self.code_input.text().strip()
        if not code:
            QMessageBox.warning(self, "激活", "请输入激活码。")
            return
        if self.runner.is_running("activate_license"):
            QMessageBox.information(self, "激活", "正在激活，请稍等。")
            return
        self.status_label.setText("正在激活...")
        self.status_label.setStyleSheet("color: #2563eb; font-weight: 700;")

        def task() -> dict:
            return activation_service.activate(code)

        def on_success(status: dict) -> None:
            self.refresh_license(show_message=False)
            QMessageBox.information(self, "激活", f"激活成功：{status['statusText']}")
            if self.activation_required and status.get("accessAllowed"):
                self.accept()

        def on_error(message: str) -> None:
            self.refresh_license(show_message=False)
            QMessageBox.warning(self, "激活失败", message)

        self.runner.run("activate_license", task, on_success=on_success, on_error=on_error)


class DoctorDialog(QDialog):
    """运行环境体检弹窗。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("运行环境体检")
        self.resize(820, 560)
        self.runner = BackgroundTaskRunner(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 20)
        layout.setSpacing(12)
        top = QHBoxLayout()
        heading = QLabel("运行环境体检")
        heading.setObjectName("dialogTitle")
        top.addWidget(heading)
        top.addStretch()
        run_btn = button("开始体检", variant="primary")
        run_btn.clicked.connect(self.run_doctor)
        top.addWidget(run_btn)
        layout.addLayout(top)
        self.output = QPlainTextEdit()
        self.output.setObjectName("diagnosticOutput")
        self.output.setReadOnly(True)
        self.output.setPlainText(
            "尚未开始体检。\n\n"
            "点击右上角“开始体检”，系统会检查运行环境、浏览器组件和关键目录。"
        )
        layout.addWidget(self.output)

    def run_doctor(self) -> None:
        if self.runner.is_running("doctor"):
            QMessageBox.information(self, "运行环境体检", "体检正在进行，请稍等。")
            return
        self.output.setPlainText("正在运行体检，请稍等...\n")

        def task() -> str:
            result = subprocess.run(
                [sys.executable, str(ROOT_DIR / "scripts" / "doctor.py"), "--skip-compile"],
                cwd=ROOT_DIR,
                text=True,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
            )
            text = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
            return text or f"体检完成，退出码：{result.returncode}"

        self.runner.run(
            "doctor",
            task,
            on_success=self.output.setPlainText,
            on_error=lambda message: self.output.setPlainText(f"体检失败：{message}"),
        )


class PackageCheckDialog(QDialog):
    """打包前检查弹窗。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("打包前检查")
        self.resize(820, 560)
        self.runner = BackgroundTaskRunner(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 20)
        layout.setSpacing(12)
        top = QHBoxLayout()
        heading = QLabel("打包前检查")
        heading.setObjectName("dialogTitle")
        top.addWidget(heading)
        top.addStretch()
        run_btn = button("开始检查", variant="warning")
        run_btn.clicked.connect(self.run_check)
        top.addWidget(run_btn)
        layout.addLayout(top)
        self.output = QPlainTextEdit()
        self.output.setObjectName("diagnosticOutput")
        self.output.setReadOnly(True)
        self.output.setPlainText(
            "尚未开始检查。\n\n"
            "点击右上角“开始检查”，系统会核对打包所需文件与运行环境。"
        )
        layout.addWidget(self.output)

    def run_check(self) -> None:
        if self.runner.is_running("package_check"):
            QMessageBox.information(self, "打包前检查", "检查正在进行，请稍等。")
            return
        self.output.setPlainText("正在运行打包前检查，请稍等...\n")

        def task() -> str:
            result = subprocess.run(
                [sys.executable, str(ROOT_DIR / "scripts" / "package_check.py"), "--mode", "runtime"],
                cwd=ROOT_DIR,
                text=True,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
            )
            text = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
            return text or f"检查完成，退出码：{result.returncode}"

        self.runner.run(
            "package_check",
            task,
            on_success=self.output.setPlainText,
            on_error=lambda message: self.output.setPlainText(f"检查失败：{message}"),
        )


class AboutDialog(QDialog):
    """关于弹窗。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("关于")
        self.resize(520, 260)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)
        heading = QLabel("关于发射台")
        heading.setObjectName("dialogTitle")
        layout.addWidget(heading)
        text = QLabel(
            f"{APP_TITLE}\n版本 {APP_VERSION}\n\n"
            "账号、素材与多平台发布任务统一工作台"
        )
        text.setWordWrap(True)
        layout.addWidget(text)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        close.rejected.connect(self.reject)
        layout.addWidget(close)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(*DEFAULT_MAIN_WINDOW_SIZE)
        self.setMinimumSize(1180, 720)
        self._apply_initial_window_geometry()

        self.dashboard = DashboardPage()
        self.accounts = AccountPage()
        self.media = MediaPage()
        self.publish = PublishPage()
        self.tasks = TaskPage()
        self.page_definitions = (
            ("工作台", self.dashboard, "ui/assets/nav-dashboard.svg"),
            ("账号管理", self.accounts, "ui/assets/nav-accounts.svg"),
            ("素材管理", self.media, "ui/assets/nav-media.svg"),
            ("发布中心", self.publish, "ui/assets/nav-publish.svg"),
            ("任务记录", self.tasks, "ui/assets/nav-tasks.svg"),
        )
        self.coming_soon_definitions = (
            ("数据监测", "ui/assets/nav-dashboard.svg"),
            ("海外平台", "ui/assets/nav-publish.svg"),
        )
        self.nav_buttons: list[QPushButton] = []
        self.coming_soon_buttons: list[QPushButton] = []
        self.tabs = QStackedWidget()
        self.tabs.setObjectName("mainStack")
        for _label, page, _icon in self.page_definitions:
            self.tabs.addWidget(page)
        self.tabs.currentChanged.connect(self._page_changed)
        self.setCentralWidget(self._build_shell())
        self._set_current_page(0)
        self.menuBar().setVisible(False)
        # 启动时不自动检测账号，避免在用户未发起操作时触发平台访问。

    def _apply_initial_window_geometry(self) -> None:
        """Windows 使用大屏自适应尺寸，其他平台保留原始窗口大小。"""

        if sys.platform != "win32":
            return
        screen = self.screen()
        if screen is None:
            return
        available = screen.availableGeometry()
        self.setGeometry(
            *calculate_windows_main_window_geometry(
                available.x(),
                available.y(),
                available.width(),
                available.height(),
            )
        )

    def _build_shell(self) -> QWidget:
        shell = QWidget()
        shell.setObjectName("appShell")
        shell_layout = QHBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("appSidebar")
        sidebar.setFixedWidth(196)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(18, 20, 18, 18)
        sidebar_layout.setSpacing(8)

        brand = QFrame()
        brand.setObjectName("brandBlock")
        brand_layout = QHBoxLayout(brand)
        brand_layout.setContentsMargins(0, 0, 0, 16)
        brand_layout.setSpacing(11)
        icon_label = QLabel()
        icon_label.setObjectName("brandIcon")
        icon_label.setFixedSize(38, 38)
        icon_path = ROOT_DIR / APP_ICON_RELATIVE_PATH
        if icon_path.exists():
            pixmap = QPixmap(str(icon_path))
            icon_label.setPixmap(
                pixmap.scaled(
                    34,
                    34,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        brand_title = QLabel("一键发")
        brand_title.setObjectName("brandTitle")
        brand_subtitle = QLabel("多平台内容发布工作台")
        brand_subtitle.setObjectName("brandSubtitle")
        brand_text.addWidget(brand_title)
        brand_text.addWidget(brand_subtitle)
        brand_layout.addWidget(icon_label)
        brand_layout.addLayout(brand_text, 1)
        sidebar_layout.addWidget(brand)

        workspace_label = QLabel("工作区")
        workspace_label.setObjectName("navSectionLabel")
        sidebar_layout.addWidget(workspace_label)

        nav_group = QButtonGroup(self)
        nav_group.setExclusive(True)
        for index, (label, _page, icon_relative_path) in enumerate(
            self.page_definitions
        ):
            nav_button = QPushButton(label)
            nav_button.setObjectName("navButton")
            nav_button.setProperty("navigation", True)
            nav_button.setCheckable(True)
            nav_button.setIcon(QIcon(str(ROOT_DIR / icon_relative_path)))
            nav_button.setIconSize(QSize(18, 18))
            nav_button.setCursor(Qt.CursorShape.PointingHandCursor)
            nav_button.clicked.connect(
                lambda _checked=False, current_index=index: self._set_current_page(
                    current_index
                )
            )
            nav_group.addButton(nav_button, index)
            self.nav_buttons.append(nav_button)
            sidebar_layout.addWidget(nav_button)

        addon_label = QLabel("增值功能")
        addon_label.setObjectName("navSectionLabel")
        sidebar_layout.addWidget(addon_label)
        for label, icon_relative_path in self.coming_soon_definitions:
            feature_button = QPushButton(f"{label} · 开发中")
            feature_button.setObjectName("navButton")
            feature_button.setProperty("navigation", True)
            feature_button.setProperty("comingSoon", True)
            feature_button.setIcon(QIcon(str(ROOT_DIR / icon_relative_path)))
            feature_button.setIconSize(QSize(18, 18))
            feature_button.setCursor(Qt.CursorShape.PointingHandCursor)
            feature_button.clicked.connect(
                lambda _checked=False, feature_name=label: self._show_coming_soon(
                    feature_name
                )
            )
            self.coming_soon_buttons.append(feature_button)
            sidebar_layout.addWidget(feature_button)

        sidebar_layout.addStretch()
        feedback_card = QFrame()
        feedback_card.setObjectName("feedbackCard")
        feedback_layout = QVBoxLayout(feedback_card)
        feedback_layout.setContentsMargins(12, 10, 12, 10)
        feedback_layout.setSpacing(4)
        feedback_title = QLabel("问题反馈")
        feedback_title.setObjectName("feedbackTitle")
        feedback_copy = QLabel(
            "产品仍在持续优化中。使用中遇到任何问题，"
            "或有功能优化建议，欢迎联系我。"
        )
        feedback_copy.setObjectName("feedbackCopy")
        feedback_copy.setWordWrap(True)
        feedback_wechat = QLabel("微信：tianyiswl")
        feedback_wechat.setObjectName("feedbackContact")
        feedback_store = QLabel("淘宝店铺：逆浪风")
        feedback_store.setObjectName("feedbackContact")
        feedback_layout.addWidget(feedback_title)
        feedback_layout.addWidget(feedback_copy)
        feedback_layout.addSpacing(3)
        feedback_layout.addWidget(feedback_wechat)
        feedback_layout.addWidget(feedback_store)
        sidebar_layout.addWidget(feedback_card)
        sidebar_layout.addSpacing(8)
        local_badge = QFrame()
        local_badge.setObjectName("localWorkspaceBadge")
        local_badge_layout = QVBoxLayout(local_badge)
        local_badge_layout.setContentsMargins(12, 10, 12, 10)
        local_badge_layout.setSpacing(2)
        local_title = QLabel("安全预检模式")
        local_title.setObjectName("localWorkspaceTitle")
        local_version = QLabel("最终发布需人工确认")
        local_version.setObjectName("localWorkspaceVersion")
        local_badge_layout.addWidget(local_title)
        local_badge_layout.addWidget(local_version)
        sidebar_layout.addWidget(local_badge)
        shell_layout.addWidget(sidebar)

        content = QFrame()
        content.setObjectName("contentShell")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        content_layout.addWidget(self._build_utility_bar())
        content_layout.addWidget(self.tabs, 1)
        shell_layout.addWidget(content, 1)
        return shell

    def _build_utility_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("utilityBar")
        bar.setFixedHeight(48)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(22, 0, 22, 0)
        layout.setSpacing(4)

        self.current_workspace_label = QLabel("工作台")
        self.current_workspace_label.setObjectName("currentWorkspaceLabel")
        layout.addWidget(self.current_workspace_label)
        layout.addStretch()

        license_button = QToolButton()
        license_button.setText("授权")
        license_button.setIcon(
            QIcon(str(ROOT_DIR / "ui" / "assets" / "shield-check.svg"))
        )
        license_button.clicked.connect(lambda: LicenseDialog(self).exec())
        layout.addWidget(license_button)

        tools_button = QToolButton()
        tools_button.setText("工具")
        tools_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        tools_button.setMenu(self._tools_menu(tools_button))
        layout.addWidget(tools_button)

        data_button = QToolButton()
        data_button.setText("数据")
        data_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        data_button.setMenu(self._data_menu(data_button))
        layout.addWidget(data_button)

        help_button = QToolButton()
        help_button.setText("帮助")
        help_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        help_button.setMenu(self._help_menu(help_button))
        layout.addWidget(help_button)
        return bar

    def _tools_menu(self, parent: QWidget) -> QMenu:
        menu = QMenu(parent)
        doctor_action = menu.addAction("运行环境体检")
        doctor_action.triggered.connect(lambda: DoctorDialog(self).exec())
        package_action = menu.addAction("打包前检查")
        package_action.triggered.connect(lambda: PackageCheckDialog(self).exec())
        menu.addSeparator()
        refresh_action = menu.addAction("刷新当前页面")
        refresh_action.triggered.connect(
            lambda: self._refresh_active_page(self.tabs.currentIndex())
        )
        return menu

    def _data_menu(self, parent: QWidget) -> QMenu:
        menu = QMenu(parent)
        for title, path in (
            ("打开素材目录", VIDEO_DIR),
            ("打开账号登录目录", COOKIE_DIR),
            ("打开头像目录", AVATAR_DIR),
            ("打开日志目录", LOG_DIR),
            ("打开数据库目录", DB_PATH.parent),
        ):
            action = menu.addAction(title)
            action.triggered.connect(
                lambda _checked=False, current_path=path: self.open_path(current_path)
            )
        return menu

    def _help_menu(self, parent: QWidget) -> QMenu:
        menu = QMenu(parent)
        help_action = menu.addAction("使用说明")
        help_action.triggered.connect(lambda: HelpDialog(self).exec())
        about_action = menu.addAction("关于")
        about_action.triggered.connect(lambda: AboutDialog(self).exec())
        return menu

    def _set_current_page(self, index: int) -> None:
        self.tabs.setCurrentIndex(index)
        self._refresh_active_page(index)
        for button_index, nav_button in enumerate(self.nav_buttons):
            nav_button.setChecked(button_index == index)
        if hasattr(self, "current_workspace_label"):
            self.current_workspace_label.setText(self.page_definitions[index][0])

    def _page_changed(self, index: int) -> None:
        for button_index, nav_button in enumerate(self.nav_buttons):
            nav_button.setChecked(button_index == index)
        if hasattr(self, "current_workspace_label"):
            self.current_workspace_label.setText(self.page_definitions[index][0])
        self._refresh_active_page(index)

    def _show_coming_soon(self, feature_name: str) -> None:
        QMessageBox.information(
            self,
            "功能开发中",
            f"{feature_name}功能还在开发中。\n\n详情联系淘宝店铺：{UPGRADE_STORE}",
        )

    def closeEvent(self, event) -> None:
        self.accounts.stop_auto_checking()
        account_browser_service.close_all_backend_sessions(wait=True)
        super().closeEvent(event)

    def _refresh_active_page(self, index: int) -> None:
        page = self.tabs.widget(index)
        refresh = getattr(page, "refresh", None)
        if callable(refresh):
            refresh()

    def open_path(self, path: Path) -> None:
        target = path if path.suffix == "" else path.parent
        target.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform.startswith("win"):
                import os

                os.startfile(str(target))
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(target)])
        except Exception as exc:
            QMessageBox.warning(self, "打开目录", f"打开失败：{exc}")
