# -*- coding: utf-8 -*-
"""账号管理页面。"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QSize, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from app_core import account_browser_service, account_service
from app_core.paths import AVATAR_DIR

from .background_task import BackgroundTaskRunner
from .common import PLATFORM_COLORS, button, table_item
from .login_dialog import LoginDialog


ACCOUNT_AVATAR_SIZE = 36


def _account_avatar_path(row: dict) -> Path | None:
    """兼容头像绝对路径和数据库中保存的头像文件名。"""

    value = str(row.get("avatarPath") or "").strip()
    if not value:
        return None
    stored_path = Path(value)
    candidates = (
        stored_path,
        AVATAR_DIR / stored_path.name,
    )
    return next((path for path in candidates if path.is_file()), None)


def _account_avatar_icon(row: dict, size: int = ACCOUNT_AVATAR_SIZE) -> QIcon:
    """生成圆形账号头像；头像缺失时显示平台色昵称首字占位。"""

    canvas = QPixmap(size, size)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    clip_path = QPainterPath()
    clip_path.addEllipse(1, 1, size - 2, size - 2)
    painter.setClipPath(clip_path)

    avatar_path = _account_avatar_path(row)
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
        platform_name = str(row.get("platformName") or "")
        painter.fillPath(
            clip_path,
            QColor(PLATFORM_COLORS.get(platform_name, "#667085")),
        )
        display_name = str(
            row.get("userName") or row.get("profileName") or platform_name or "账"
        ).strip()
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
    return QIcon(canvas)


def _account_identity_cell(row: dict) -> QWidget:
    """只呈现头像与账号名，避免账号列混入状态或系统说明。"""

    cell = QWidget()
    layout = QHBoxLayout(cell)
    layout.setContentsMargins(8, 4, 6, 4)
    layout.setSpacing(8)
    avatar = QLabel()
    avatar.setFixedSize(ACCOUNT_AVATAR_SIZE, ACCOUNT_AVATAR_SIZE)
    avatar.setPixmap(
        _account_avatar_icon(row).pixmap(QSize(ACCOUNT_AVATAR_SIZE, ACCOUNT_AVATAR_SIZE))
    )
    avatar.setToolTip("平台头像" if _account_avatar_path(row) else "默认头像；可在操作中刷新账号信息")
    layout.addWidget(avatar)
    details = QVBoxLayout()
    details.setContentsMargins(0, 0, 0, 0)
    name = QLabel(str(row.get("userName") or row.get("profileName") or "未命名账号"))
    name.setProperty("role", "accountName")
    details.addWidget(name)
    layout.addLayout(details, 1)
    return cell


class AccountPage(QWidget):
    AUTO_CHECK_INTERVAL_MS = 24 * 60 * 60 * 1000
    validation_progress_changed = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("pageRoot")
        self.tasks = BackgroundTaskRunner(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(14)

        header = QHBoxLayout()
        title = QLabel("账号管理")
        title.setObjectName("pageTitle")
        header.addWidget(title)
        self.result_label = QLabel("0 个账号")
        self.result_label.setProperty("role", "countBadge")
        header.addWidget(self.result_label)
        header.addStretch()

        browser_mode_hint = QLabel("绑定显示官方页 · 检测默认静默")
        browser_mode_hint.setProperty("role", "muted")
        browser_mode_hint.setToolTip(
            "绑定和重新登录需要用户操作，始终显示官方页面；"
            "检测登录始终在后台运行，异常时只更新状态并提示手动重新登录。"
        )
        header.addWidget(browser_mode_hint)

        self.check_all_btn = button("检测登录", variant="secondary")
        self.check_all_btn.clicked.connect(self.check_all)
        header.addWidget(self.check_all_btn)

        self.refresh_btn = button("刷新", variant="secondary")
        self.refresh_btn.clicked.connect(self.refresh)
        header.addWidget(self.refresh_btn)

        bind_btn = button("绑定账号", variant="primary")
        bind_btn.setToolTip(
            "在一键发独立浏览器会话中登录平台；"
            "不会读取蚁小二客户端的账号或登录态。"
        )
        bind_btn.clicked.connect(self.bind_account)
        header.addWidget(bind_btn)
        layout.addLayout(header)

        filters = QFrame()
        filters.setProperty("toolbar", True)
        filter_layout = QGridLayout(filters)
        filter_layout.setContentsMargins(14, 12, 14, 12)
        filter_layout.setHorizontalSpacing(10)
        filter_layout.setVerticalSpacing(5)

        self.profile_filter = QComboBox()
        self.profile_filter.currentIndexChanged.connect(self.refresh)
        profile_label = QLabel("账号主体")
        profile_label.setProperty("role", "caption")
        filter_layout.addWidget(profile_label, 0, 0)
        filter_layout.addWidget(self.profile_filter, 1, 0)

        self.account_platform_filter = QComboBox()
        self.account_platform_filter.currentIndexChanged.connect(self.refresh)
        platform_label = QLabel("发布平台")
        platform_label.setProperty("role", "caption")
        filter_layout.addWidget(platform_label, 0, 1)
        filter_layout.addWidget(self.account_platform_filter, 1, 1)

        self.account_search = QLineEdit()
        self.account_search.setPlaceholderText("搜索主体、账号名或备注")
        self.account_search.textChanged.connect(self.refresh)
        search_label = QLabel("搜索")
        search_label.setProperty("role", "caption")
        filter_layout.addWidget(search_label, 0, 2)
        filter_layout.addWidget(self.account_search, 1, 2)
        filter_layout.setColumnStretch(2, 1)

        self.status_filter = QComboBox()
        self.status_filter.addItem("全部状态", None)
        self.status_filter.addItem("正常", "normal")
        self.status_filter.addItem("已登录待检测", "pending")
        self.status_filter.addItem("待检测", "stale")
        self.status_filter.addItem("异常", "abnormal")
        self.status_filter.currentIndexChanged.connect(self.refresh)
        status_filter_label = QLabel("登录状态")
        status_filter_label.setProperty("role", "caption")
        filter_layout.addWidget(status_filter_label, 0, 3)
        filter_layout.addWidget(self.status_filter, 1, 3)
        layout.addWidget(filters)

        self.status_label = QLabel("")
        self.status_label.setObjectName("inlineStatus")
        self.status_label.setMinimumHeight(30)
        self.status_label.setVisible(False)
        layout.addWidget(self.status_label)

        self.validation_progress = QProgressBar()
        self.validation_progress.setObjectName("accountValidationProgress")
        self.validation_progress.setRange(0, 1)
        self.validation_progress.setValue(0)
        self.validation_progress.setFormat("等待检测")
        self.validation_progress.setTextVisible(False)
        self.validation_progress.setFixedHeight(22)
        self.validation_progress.setVisible(False)
        layout.addWidget(self.validation_progress)

        self.validation_elapsed_seconds = 0
        self.validation_progress_state: dict = {}
        self.validation_progress_timer = QTimer(self)
        self.validation_progress_timer.setInterval(1000)
        self.validation_progress_timer.timeout.connect(
            self._tick_validation_progress
        )
        self.validation_progress_changed.connect(
            self._update_validation_progress
        )

        self.table = QTableWidget(0, 6)
        self.table.setObjectName("dataTable")
        self.table.setHorizontalHeaderLabels(["主体", "平台", "状态", "账号名", "备注", "操作"])
        table_header = self.table.horizontalHeader()
        table_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        table_header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 130)
        self.table.setColumnWidth(1, 95)
        self.table.setColumnWidth(2, 78)
        self.table.setColumnWidth(3, 135)
        self.table.setColumnWidth(5, 150)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setIconSize(QSize(ACCOUNT_AVATAR_SIZE, ACCOUNT_AVATAR_SIZE))
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.open_menu)
        layout.addWidget(self.table)
        QTimer.singleShot(0, self._apply_table_column_widths)

        self.auto_check_timer = QTimer(self)
        self.auto_check_timer.setInterval(self.AUTO_CHECK_INTERVAL_MS)
        self.auto_check_timer.timeout.connect(self.auto_check_stale_accounts)
        self.initial_auto_check_timer = QTimer(self)
        self.initial_auto_check_timer.setSingleShot(True)
        self.initial_auto_check_timer.timeout.connect(
            self.auto_check_stale_accounts
        )
        self.refresh()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_table_column_widths()

    def _apply_table_column_widths(self) -> None:
        """窄窗口收紧固定列，优先保证账号名与备注可读。"""

        compact = self.table.viewport().width() < 1180
        widths = (108, 82, 70, 150, 138) if compact else (130, 95, 78, 175, 150)
        for column, width in zip((0, 1, 2, 3, 5), widths):
            self.table.setColumnWidth(column, width)

    def start_auto_checking(self) -> None:
        """启动全局账号后台复检，不依赖账号管理页是否可见。"""

        if not self.auto_check_timer.isActive():
            self.auto_check_timer.start()
            self.initial_auto_check_timer.start(500)

    def stop_auto_checking(self) -> None:
        self.initial_auto_check_timer.stop()
        self.auto_check_timer.stop()

    def refresh(self) -> None:
        rows = account_service.list_managed_accounts()
        self._refresh_filters()

        selected_profile = self.profile_filter.currentText()
        if selected_profile and selected_profile != "全部主体":
            rows = [row for row in rows if row["profileName"] == selected_profile]

        selected_platform = self.account_platform_filter.currentData()
        if selected_platform is not None:
            rows = [row for row in rows if row["type"] == selected_platform]

        selected_status = self.status_filter.currentData()
        if selected_status is not None:
            rows = [
                row
                for row in rows
                if row.get("healthStatus") == selected_status
            ]

        keyword = self.account_search.text().strip().lower()
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in self._searchable_text(row)
            ]

        self.table.clearSpans()
        self.table.setRowCount(len(rows))
        self.result_label.setText(f"{len(rows)} 个账号")
        for row_idx, row in enumerate(rows):
            self.table.setItem(row_idx, 0, table_item(row["profileName"]))
            self.table.setItem(row_idx, 1, table_item(row["platformName"], PLATFORM_COLORS.get(row["platformName"])))
            color = {
                "normal": "#059669",
                "pending": "#2563eb",
                "stale": "#B54708",
                "abnormal": "#dc2626",
            }.get(row.get("healthStatus"), "#dc2626")
            self.table.setItem(row_idx, 2, table_item(row["statusText"], color))
            account_item = table_item("")
            avatar_updated_at = row.get("avatarUpdatedAt") or "未获取"
            account_item.setToolTip(f"头像更新时间：{avatar_updated_at}")
            self.table.setItem(row_idx, 3, account_item)
            self.table.setCellWidget(row_idx, 3, _account_identity_cell(row))
            self.table.setItem(row_idx, 4, table_item(row["remark"]))
            self.table.setCellWidget(row_idx, 5, self._actions(row))
            self.table.item(row_idx, 0).setData(Qt.ItemDataRole.UserRole, row)
            self.table.setRowHeight(row_idx, 54)
        if not rows:
            self.table.setRowCount(1)
            empty = table_item("暂无账号")
            empty.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(0, 0, empty)
            self.table.setSpan(0, 0, 1, self.table.columnCount())
            self.table.setRowHeight(0, 52)

    def _searchable_text(self, row: dict) -> str:
        return " ".join(
            str(row.get(key) or "")
            for key in ("profileName", "platformName", "statusText", "userName", "remark")
        ).lower()

    def _refresh_filters(self) -> None:
        current_profile = self.profile_filter.currentText()
        profiles = ["全部主体"] + account_service.list_profiles()
        self.profile_filter.blockSignals(True)
        self.profile_filter.clear()
        self.profile_filter.addItems(profiles)
        if current_profile in profiles:
            self.profile_filter.setCurrentText(current_profile)
        self.profile_filter.blockSignals(False)

        current_platform = self.account_platform_filter.currentData()
        self.account_platform_filter.blockSignals(True)
        self.account_platform_filter.clear()
        self.account_platform_filter.addItem("全部平台", None)
        for platform_type in account_service.PLATFORM_ORDER:
            self.account_platform_filter.addItem(account_service.PLATFORMS[platform_type], platform_type)
        if current_platform is not None:
            index = self.account_platform_filter.findData(current_platform)
            if index >= 0:
                self.account_platform_filter.setCurrentIndex(index)
        self.account_platform_filter.blockSignals(False)

    def _actions(self, row: dict) -> QWidget:
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(4, 5, 4, 5)
        layout.setSpacing(4)
        menu = QMenu(box)
        is_youtube_oauth = (
            row.get("authMode") == account_service.AUTH_MODE_YOUTUBE_OAUTH
        )
        needs_youtube_scope_upgrade = (
            is_youtube_oauth
            and int(row.get("oauthScopeVersion") or 1) < 2
        )
        menu.addAction("检测登录状态", lambda _checked=False, r=row: self.check_one(r))
        menu.addAction(
            "升级 YouTube 发布权限"
            if needs_youtube_scope_upgrade
            else "重新登录",
            lambda _checked=False, r=row: self.relogin(r),
        )
        refresh_action = menu.addAction(
            "刷新账号信息",
            lambda _checked=False, r=row: self.refresh_avatar(r),
        )
        refresh_action.setToolTip(
            "从 YouTube 官方 API 刷新频道名和头像"
            if is_youtube_oauth
            else "从当前已登录的官方后台刷新账号信息"
        )
        menu.addAction("编辑备注", lambda _checked=False, r=row: self.edit_remark(r))
        menu.addSeparator()
        menu.addAction("删除账号", lambda _checked=False, r=row: self.delete_one(r))

        open_backend_btn = button("打开后台", variant="primary", compact=True)
        if row.get("needsPageRebind"):
            open_backend_btn.setEnabled(False)
            open_backend_btn.setToolTip(
                "旧 Facebook Page 记录没有精确 Page ID，请先重新登录绑定。"
            )
        open_backend_btn.setToolTip(
            "使用系统默认浏览器打开该频道的 YouTube Studio"
            if is_youtube_oauth
            else open_backend_btn.toolTip()
            or "使用一键发保存的本地会话打开对应平台官网"
        )
        open_backend_btn.clicked.connect(
            lambda _checked=False, r=row: self.open_backend(r)
        )
        more_btn = button("更多", variant="secondary", compact=True)
        more_btn.setToolTip("打开其它账号操作")
        more_btn.clicked.connect(
            lambda _checked=False, b=more_btn, m=menu: m.exec(
                b.mapToGlobal(b.rect().bottomLeft())
            )
        )
        layout.addWidget(open_backend_btn)
        layout.addWidget(more_btn)
        return box

    def row_data(self, row_index: int) -> dict | None:
        item = self.table.item(row_index, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def open_menu(self, pos) -> None:
        index = self.table.indexAt(pos)
        if index.isValid():
            self.table.selectRow(index.row())
        row_index = self.table.currentRow()
        row = self.row_data(row_index)
        if not row:
            return
        menu = QMenu(self)
        is_youtube_oauth = (
            row.get("authMode") == account_service.AUTH_MODE_YOUTUBE_OAUTH
        )
        needs_youtube_scope_upgrade = (
            is_youtube_oauth
            and int(row.get("oauthScopeVersion") or 1) < 2
        )
        menu.addAction(
            "升级 YouTube 发布权限"
            if needs_youtube_scope_upgrade
            else "重新登录",
            lambda: self.relogin(row),
        )
        open_action = menu.addAction("打开后台", lambda: self.open_backend(row))
        open_action.setToolTip(
            "使用系统默认浏览器打开该频道的 YouTube Studio"
            if is_youtube_oauth
            else "使用一键发保存的本地会话打开对应平台官网"
        )
        menu.addAction("检测登录", lambda: self.check_one(row))
        refresh_action = menu.addAction(
            "刷新头像/登录信息",
            lambda: self.refresh_avatar(row),
        )
        refresh_action.setToolTip(
            "从 YouTube 官方 API 刷新频道名和头像"
            if is_youtube_oauth
            else "从当前已登录的官方后台刷新账号信息"
        )
        menu.addAction("编辑备注", lambda: self.edit_remark(row))
        menu.addAction("删除账号", lambda: self.delete_one(row))
        menu.exec(self.table.mapToGlobal(pos))

    def bind_account(self) -> None:
        dialog = LoginDialog(self, background_login=True)
        result = dialog.exec()
        self.refresh()
        if dialog.lifecycle_message:
            self._set_status(dialog.lifecycle_message)

    def relogin(self, row: dict) -> None:
        dialog = LoginDialog(
            self,
            row,
            background_login=True,
        )
        result = dialog.exec()
        self.refresh()
        if dialog.lifecycle_message:
            self._set_status(dialog.lifecycle_message)

    def check_all(self) -> None:
        self.start_validation(None)

    def check_one(self, row: dict) -> None:
        self.start_validation([row["id"]])

    def auto_check_stale_accounts(self) -> None:
        """超过可信时限后先静默检测 Cookie，失败才标记为待检测。"""

        stale_ids = account_service.accounts_requiring_check()
        if stale_ids:
            self.start_validation(
                stale_ids,
                silent=True,
                invalid_status=2,
            )

    def start_validation(
        self,
        account_ids: list[int] | None,
        *,
        silent: bool = False,
        invalid_status: int = 0,
    ) -> None:
        if self.tasks.is_running("account_validation"):
            if not silent:
                QMessageBox.information(self, "检测登录", "账号检测正在进行，请稍等。")
            return

        started = self.tasks.run(
            "account_validation",
            lambda: account_service.validate_accounts(
                account_ids,
                progress_callback=self.validation_progress_changed.emit,
                invalid_status=invalid_status,
            ),
            on_started=lambda: self._set_validation_running(True),
            on_success=lambda payload: self._finish_validation(payload, silent=silent),
            on_error=lambda message: self._fail_validation(message, silent=silent),
            on_finished=lambda: self._set_validation_running(False),
        )
        if started:
            scope = (
                f"{len(account_ids)} 个待检测账号"
                if silent and account_ids
                else "当前账号"
                if account_ids
                else "全部账号"
            )
            self._set_status(f"正在检测{scope}登录状态，请稍等...")

    def _set_validation_running(self, running: bool) -> None:
        self.check_all_btn.setEnabled(not running)
        if running:
            self.check_all_btn.setText("检测中…")
            self.validation_elapsed_seconds = 0
            self.validation_progress_state = {
                "phase": "preparing",
                "text": "正在准备账号检测",
            }
            self.validation_progress.setRange(0, 0)
            self.validation_progress.setVisible(True)
            self.validation_progress_timer.start()
            self._render_validation_progress()
            self._set_status("正在检测账号登录状态，请稍等...")
        else:
            self.check_all_btn.setText("检测登录")
            self.validation_progress_timer.stop()
            self.validation_progress.setVisible(False)
            self.validation_progress_state = {}

    def _update_validation_progress(self, event: object) -> None:
        if not isinstance(event, dict):
            return
        self.validation_progress_state = dict(event)
        current = max(0, int(event.get("current") or 0))
        total = max(0, int(event.get("total") or 0))
        if total:
            self.validation_progress.setRange(0, total)
            self.validation_progress.setValue(
                current if event.get("phase") == "checked" else max(0, current - 1)
            )
            self.check_all_btn.setText(f"检测中 {current}/{total}")
        self._render_validation_progress()

    def _tick_validation_progress(self) -> None:
        self.validation_elapsed_seconds += 1
        self._render_validation_progress()

    def _render_validation_progress(self) -> None:
        event = self.validation_progress_state
        phase = event.get("phase")
        current = int(event.get("current") or 0)
        total = int(event.get("total") or 0)
        platform = str(event.get("platformName") or "")
        profile = str(event.get("profileName") or "")
        elapsed = f"已用时 {self.validation_elapsed_seconds} 秒"
        if phase == "checking":
            text = f"正在检测 {current}/{total}：{platform} | {profile} · {elapsed}"
        elif phase == "checked":
            result = "正常" if event.get("valid") else "异常"
            text = f"已检测 {current}/{total}：{platform} {result} · {elapsed}"
        else:
            text = f"{event.get('text') or '正在准备账号检测'} · {elapsed}"
        self.validation_progress.setFormat(text)
        self._set_status(text)

    def _finish_validation(self, payload: dict, *, silent: bool = False) -> None:
        self.refresh()
        normal = payload["normal"]
        abnormal = payload["abnormal"]
        pending = payload.get("pending", [])
        prefix = "自动复检完成" if silent else "检测完成"
        self._set_status(
            f"{prefix}：正常 {len(normal)} 个，已登录待检测 {len(pending)} 个，异常 {len(abnormal)} 个。"
        )
        if self._present_validation_intervention(payload):
            return
        if silent:
            return
        lines = [
            f"检测完成：正常 {len(normal)} 个，已登录待检测 {len(pending)} 个，异常 {len(abnormal)} 个。"
        ]
        for row in payload["checked"]:
            lines.append(f"{row['platformName']}：{row['statusText']}（{row['profileName']} | {row['userName']}）")
        if payload["failures"]:
            lines.append("")
            lines.extend(payload["failures"])
        QMessageBox.information(self, "检测登录", "\n".join(lines))

    def _present_validation_intervention(self, payload: dict) -> bool:
        """异常时只提示手动处理，不从检测流程自动打开登录页。"""

        rows = list(payload.get("interventionRequired") or [])
        if not rows:
            return False
        first = rows[0]
        platform = str(first.get("platformName") or "平台")
        account_name = str(first.get("userName") or first.get("profileName") or "账号")
        remaining = (
            f"\n另有 {len(rows) - 1} 个异常账号，请检查列表。"
            if len(rows) > 1
            else ""
        )
        issue_code = str(first.get("authIssueCode") or "")
        if issue_code == "youtube_channel_identity_mismatch":
            detail = (
                "当前授权返回的 YouTube 频道身份不一致，"
                "已停止替换原账号。\n"
                "请在该账号的操作菜单中重新授权原频道。"
            )
        elif issue_code == "youtube_authorization_invalid":
            detail = (
                "YouTube 官方授权已失效。\n"
                "请在该账号的操作菜单中重新授权。"
            )
        else:
            detail = (
                "本次检测只更新账号状态，不会打开平台登录页。\n"
                "如需恢复会话，请在该账号的操作菜单中点击“重新登录”。"
            )
        QMessageBox.warning(
            self,
            "登录状态需处理",
            f"{platform} | {account_name} 的会话未通过静默检测。"
            f"{remaining}\n\n{detail}",
        )
        return True

    def _fail_validation(self, message: str, *, silent: bool = False) -> None:
        self._set_status("检测失败，请查看提示后重试。")
        if not silent:
            QMessageBox.warning(self, "检测登录", message)

    def open_backend(self, row: dict) -> None:
        try:
            reused = account_browser_service.open_account_backend(row)
        except Exception as exc:
            self._set_status(f"无法打开后台：{row['platformName']}")
            QMessageBox.warning(self, "打开平台后台", str(exc))
            return
        action = "已切换到现有后台" if reused else "已打开后台"
        self._set_status(
            f"{action}：{row['platformName']} | {row['profileName']}"
        )

    def edit_remark(self, row: dict) -> None:
        text, ok = QInputDialog.getText(self, "编辑备注", "备注：", text=row.get("remark") or "")
        if ok:
            account_service.update_remark(row["id"], text)
            self.refresh()

    def refresh_avatar(self, row: dict) -> None:
        key = f"refresh_avatar_{row['id']}"
        if self.tasks.is_running(key):
            QMessageBox.information(self, "刷新账号", "该账号正在刷新，请稍等。")
            return

        def done(refreshed: dict) -> None:
            self.refresh()
            name = refreshed.get("userName") or row.get("userName") or "账号"
            self._set_status(f"刷新完成：{row['platformName']} | {name}")
            QMessageBox.information(self, "刷新账号", f"{row['platformName']} 账号信息刷新完成。")

        started = self.tasks.run(
            key,
            lambda: account_service.refresh_account_avatar(row["id"]),
            on_started=lambda: self._set_status(
                f"正在刷新：{row['platformName']} | {row['profileName']}，请稍等..."
            ),
            on_success=done,
            on_error=lambda message: self._fail_refresh_avatar(row, message),
        )
        if started:
            self._set_status(f"正在刷新：{row['platformName']} | {row['profileName']}，请稍等...")

    def _fail_refresh_avatar(self, row: dict, message: str) -> None:
        self._set_status(f"刷新失败：{row['platformName']} | {row['profileName']}")
        QMessageBox.warning(self, "刷新账号", message)

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)
        self.status_label.setVisible(bool(text.strip()))

    def delete_one(self, row: dict) -> None:
        title = f"{row['platformName']}｜{row['userName']}"
        if QMessageBox.question(self, "删除账号", f"确定删除 {title}？") == QMessageBox.StandardButton.Yes:
            account_service.delete_account(row["id"])
            self.refresh()
