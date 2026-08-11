# -*- coding: utf-8 -*-
"""桌面端 UI 公共组件与视觉规范。"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import (
    QApplication,
    QProxyStyle,
    QPushButton,
    QStyle,
    QTabBar,
    QTableWidgetItem,
    QToolButton,
)

from app_core.branding import APP_TITLE


ROOT_DIR = Path(__file__).resolve().parents[1]

COLORS = {
    "primary": "#0B766E",
    "primary_hover": "#075F59",
    "primary_soft": "#E8F5F3",
    "accent": "#E4572E",
    "text": "#172033",
    "text_secondary": "#475467",
    "muted": "#667085",
    "border": "#DCE3EA",
    "border_strong": "#C5CFDA",
    "surface": "#FFFFFF",
    "surface_soft": "#F8FAFB",
    "canvas": "#F3F6F8",
    "success": "#067647",
    "warning": "#B54708",
    "danger": "#B42318",
    "info": "#175CD3",
}

PLATFORM_COLORS = {
    "抖音": "#182230",
    "视频号": "#175CD3",
    "B站": "#087E8B",
    "小红书": "#C1152B",
    "快手": "#D25F00",
    "TikTok": "#182230",
    "YouTube": "#D92D20",
    "Instagram Reels": "#A63A79",
    "Facebook Reels": "#175CD3",
}


class BrandFocusProxyStyle(QProxyStyle):
    """用品牌焦点样式替代 Windows 按钮文字周围的原生虚线框。"""

    _BRAND_FOCUS_WIDGETS = (QPushButton, QToolButton, QTabBar)

    @classmethod
    def uses_brand_focus_indicator(cls, widget: object | None) -> bool:
        return isinstance(widget, cls._BRAND_FOCUS_WIDGETS)

    def drawPrimitive(self, element, option, painter, widget=None) -> None:  # noqa: N802
        style_object = widget
        if style_object is None:
            style_object = getattr(option, "styleObject", None)
        if (
            element == QStyle.PrimitiveElement.PE_FrameFocusRect
            and self.uses_brand_focus_indicator(style_object)
        ):
            return
        super().drawPrimitive(element, option, painter, widget)


def _button_variant(color: str | None, variant: str | None) -> str:
    if variant:
        return variant
    normalized = (color or "").lower()
    if normalized in {"#dc2626", "#ef4444", "#b42318"}:
        return "danger"
    if normalized in {"#d97706", "#f59e0b", "#b54708"}:
        return "warning"
    if normalized in {"#059669", "#0f766e", "#16a34a"}:
        return "primary"
    return "secondary"


def button(
    text: str,
    color: str | None = None,
    *,
    variant: str | None = None,
    compact: bool = False,
) -> QPushButton:
    """创建遵循统一视觉规范的命令按钮。"""

    btn = QPushButton(text)
    btn.setProperty("variant", _button_variant(color, variant))
    btn.setProperty("compact", compact)
    btn.setMinimumHeight(30 if compact else 36)
    btn.setMinimumWidth(0 if compact else 76)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    return btn


def table_item(value: object, color: str | None = None) -> QTableWidgetItem:
    item = QTableWidgetItem("" if value is None else str(value))
    if color:
        item.setForeground(QColor(color))
    return item


def apply_style(app: QApplication) -> None:
    """应用稳定的浅色主题，避免跟随系统主题造成文字不可见。"""

    focus_style = getattr(app, "_brand_focus_style", None)
    if focus_style is None:
        focus_style = BrandFocusProxyStyle(app.style())
        app.setStyle(focus_style)
        app._brand_focus_style = focus_style

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(COLORS["canvas"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(COLORS["text"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(COLORS["surface"]))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(COLORS["surface_soft"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(COLORS["text"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(COLORS["surface"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(COLORS["text"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(COLORS["primary"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#101828"))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#FFFFFF"))
    app.setPalette(palette)

    app.setStyleSheet(
        """
        QWidget {
            font-family: "PingFang SC", "Microsoft YaHei";
            font-size: 13px;
            color: #172033;
            letter-spacing: 0;
        }
        QMainWindow, QDialog, QWidget#pageRoot { background: #F6F8FB; }
        QWidget#appShell, QFrame#contentShell, QStackedWidget#mainStack {
            background: #F6F8FB;
        }
        QFrame#appSidebar {
            background: #17243A;
            border: 0;
            border-right: 1px solid #24344D;
        }
        QFrame#brandBlock { background: transparent; border: 0; }
        QLabel#brandIcon { background: transparent; }
        QLabel#brandTitle {
            color: #FFFFFF;
            font-size: 19px;
            font-weight: 700;
        }
        QLabel#brandSubtitle {
            color: #9FAEC0;
            font-size: 10px;
        }
        QLabel#navSectionLabel {
            color: #7F91A8;
            font-size: 11px;
            font-weight: 600;
            padding: 8px 10px 4px 10px;
        }
        QPushButton#navButton {
            min-height: 42px;
            padding: 0 13px;
            border: 0;
            border-radius: 6px;
            background: transparent;
            color: #B8C4D3;
            font-weight: 600;
            text-align: left;
        }
        QPushButton#navButton:hover {
            background: #22324B;
            color: #FFFFFF;
        }
        QPushButton#navButton:checked {
            background: #E8F5F3;
            color: #075F59;
        }
        QFrame#localWorkspaceBadge {
            background: #1D304B;
            border: 1px solid #304564;
            border-radius: 9px;
        }
        QLabel#localWorkspaceTitle {
            color: #DCE4ED;
            font-size: 12px;
            font-weight: 600;
        }
        QLabel#localWorkspaceVersion {
            color: #8192A8;
            font-size: 11px;
        }
        QFrame#feedbackCard {
            background: #1A2B43;
            border: 1px solid #31435D;
            border-radius: 9px;
        }
        QLabel#feedbackTitle {
            color: #F2F6FA;
            font-size: 12px;
            font-weight: 700;
        }
        QLabel#feedbackCopy {
            color: #AAB8C9;
            font-size: 11px;
            line-height: 1.35;
        }
        QLabel#feedbackContact {
            color: #D9E4F0;
            font-size: 11px;
            font-weight: 600;
        }
        QFrame#utilityBar {
            background: #FFFFFF;
            border: 0;
            border-bottom: 1px solid #E3EAF1;
        }
        QLabel#currentWorkspaceLabel {
            color: #667085;
            font-size: 12px;
            font-weight: 600;
        }
        QFrame#utilityBar QToolButton {
            min-height: 30px;
            padding: 0 9px;
            border: 0;
            border-radius: 5px;
            background: transparent;
            color: #475467;
            font-weight: 600;
        }
        QFrame#utilityBar QToolButton:hover {
            background: #EEF2F5;
            color: #172033;
        }

        QLabel { color: #172033; background: transparent; }
        QLabel#pageTitle { font-size: 24px; font-weight: 700; color: #101828; }
        QLabel#dialogTitle { font-size: 18px; font-weight: 700; color: #111827; }
        QLabel#metricValue { font-size: 28px; font-weight: 700; color: #111827; }
        QLabel[role="sectionTitle"] { font-size: 15px; font-weight: 700; color: #172033; }
        QLabel[role="caption"] { font-size: 12px; color: #667085; }
        QLabel[role="muted"] { color: #667085; }
        QLabel[role="countBadge"] {
            color: #075F59;
            background: #E8F5F3;
            border-radius: 6px;
            padding: 3px 8px;
            font-size: 12px;
            font-weight: 600;
        }
        QLabel[role="success"] {
            color: #067647;
            background: #ECFDF3;
            border-radius: 5px;
            padding: 3px 7px;
            font-weight: 600;
        }
        QLabel[role="warning"] {
            color: #B54708;
            background: #FFFAEB;
            border-radius: 5px;
            padding: 3px 7px;
            font-weight: 600;
        }
        QLabel[role="danger"] {
            color: #B42318;
            background: #FEF3F2;
            border-radius: 5px;
            padding: 3px 7px;
            font-weight: 600;
        }
        QLabel#inlineStatus {
            color: #475467;
            background: #F8FAFB;
            border: 1px solid #E5EAF0;
            border-radius: 5px;
            padding: 7px 10px;
        }
        QLabel#mediaThumbnail {
            color: #667085;
            background: #F8FAFB;
            border: 1px solid #DCE3EA;
            border-radius: 5px;
        }
        QLabel#loginQrPanel {
            color: #475467;
            background: #FFFFFF;
            border: 1px dashed #B8C4D1;
            border-radius: 6px;
        }
        QLabel#infoCallout {
            color: #475467;
            background: #EFF6FF;
            border: 1px solid #D4E5F7;
            border-radius: 6px;
            padding: 10px 12px;
        }
        QTextEdit#loginStatusLog {
            background: #F8FAFB;
            color: #475467;
            border: 1px solid #DCE3EA;
            font-size: 12px;
        }
        QFrame#historyTagChip {
            background: #F8FAFC;
            border: 1px solid #D5DEE8;
            border-radius: 6px;
        }
        QToolButton#historyTagText {
            color: #344054;
            background: transparent;
            border: 0;
            padding: 0 2px;
        }
        QToolButton#historyTagText:hover { color: #0F766E; }
        QToolButton#historyTagDelete {
            color: #667085;
            background: transparent;
            border: 0;
            padding: 0;
        }
        QToolButton#historyTagDelete:hover {
            color: #B42318;
            background: #FEE4E2;
            border-radius: 4px;
        }
        QListWidget#historyTagList {
            background: #FFFFFF;
            border: 1px solid #DDE3EA;
            border-radius: 6px;
            padding: 6px;
            outline: none;
        }
        QListWidget#historyTagList::item {
            background: transparent;
            border: 0;
            padding: 0;
        }
        QListWidget#historyTagList::item:hover,
        QListWidget#historyTagList::item:selected {
            background: transparent;
            color: #182230;
        }

        QFrame[panel="true"], QFrame[workspace="true"], QGroupBox {
            background: #FFFFFF;
            border: 1px solid #DCE3EA;
            border-radius: 6px;
        }
        QFrame[toolbar="true"] {
            background: #FFFFFF;
            border: 1px solid #DCE3EA;
            border-radius: 6px;
        }
        QFrame[subPanel="true"] {
            background: #F8FAFB;
            border: 1px solid #E5EAF0;
            border-radius: 6px;
        }
        QFrame#metricCard {
            background: #FFFFFF;
            border: 1px solid #DCE3EA;
            border-left: 4px solid #0B766E;
            border-radius: 6px;
        }
        QFrame#metricCard[metricTone="blue"] { border-left-color: #3478C7; }
        QFrame#metricCard[metricTone="amber"] { border-left-color: #D28A21; }
        QFrame#metricCard[metricTone="coral"] { border-left-color: #E4572E; }
        QFrame#modeBar {
            background: #F8FAFB;
            border: 1px solid #DCE3EA;
            border-radius: 6px;
        }
        QFrame#publishTargetPanel,
        QFrame#publishContentPanel,
        QFrame#publishActivityPanel {
            background: #FFFFFF;
            border: 1px solid #DCE3EA;
            border-radius: 6px;
        }
        QFrame#taskProgressBar {
            background: #F8FAFB;
            border: 1px solid #E5EAF0;
            border-radius: 5px;
        }
        QGroupBox {
            margin-top: 14px;
            padding: 14px 12px 12px 12px;
            font-weight: 600;
            color: #344054;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            subcontrol-position: top left;
            left: 12px;
            padding: 0 6px;
            background: #FFFFFF;
            color: #344054;
        }

        QPushButton {
            min-height: 34px;
            padding: 0 14px;
            border: 1px solid #C9D2DD;
            border-radius: 6px;
            background: #FFFFFF;
            color: #344054;
            font-weight: 600;
        }
        QPushButton:hover { background: #F7F9FB; border-color: #98A2B3; }
        QPushButton:pressed { background: #EEF2F6; }
        QPushButton[compact="true"] { min-height: 28px; padding: 0 9px; font-size: 12px; }
        QPushButton[variant="primary"] {
            background: #0B766E;
            border-color: #0B766E;
            color: #FFFFFF;
        }
        QPushButton[variant="primary"]:hover { background: #075F59; border-color: #075F59; }
        QPushButton[variant="secondary"] { background: #FFFFFF; color: #344054; }
        QPushButton[variant="ghost"] { background: transparent; border-color: transparent; color: #475467; }
        QPushButton[variant="ghost"]:hover { background: #EEF2F6; }
        QPushButton[variant="warning"] { background: #FFFFFF; border-color: #F0B27A; color: #B54708; }
        QPushButton[variant="danger"] { background: #FFFFFF; border-color: #FDA29B; color: #B42318; }
        QPushButton:focus {
            border: 2px solid #2DD4BF;
        }
        QPushButton[variant="primary"]:focus {
            border-color: #5EEAD4;
        }
        QPushButton:disabled {
            background: #EAECF0;
            border-color: #EAECF0;
            color: #98A2B3;
        }
        QPushButton#publishTypeButton {
            min-height: 34px;
            padding: 0 16px;
            background: #FFFFFF;
            border: 1px solid #B8C5D1;
            color: #344054;
        }
        QPushButton#publishTypeButton:checked {
            background: #0B766E;
            border-color: #0B766E;
            color: #FFFFFF;
        }
        QPushButton#publishTypeButton:checked:hover {
            background: #075F59;
            border-color: #075F59;
        }
        QPushButton#publishTypeEntryButton {
            min-height: 156px;
            padding: 0 26px;
            background: #FFFFFF;
            border: 1px solid #D8E2EC;
            border-top: 4px solid #5BC8BF;
            border-radius: 16px;
            color: #182230;
            font-size: 21px;
            font-weight: 700;
        }
        QPushButton#publishTypeEntryButton:hover {
            background: #F0FAF8;
            border: 2px solid #0B766E;
            border-top: 4px solid #0B766E;
            color: #075F59;
        }
        QPushButton#publishTypeEntryButton:pressed {
            background: #D8F0EC;
            border: 2px solid #075F59;
            border-top: 4px solid #075F59;
            color: #075F59;
        }
        QPushButton#publishTypeEntryButton:focus {
            border: 2px solid #2DD4BF;
            border-top: 4px solid #0B766E;
        }
        QToolButton:focus {
            border: 1px solid #2DD4BF;
            border-radius: 5px;
        }

        QLineEdit, QComboBox, QSpinBox, QDateEdit, QTimeEdit {
            min-height: 34px;
            background: #FFFFFF;
            color: #182230;
            border: 1px solid #C5CFDA;
            border-radius: 6px;
            padding: 0 10px;
            selection-background-color: #0B766E;
            selection-color: #FFFFFF;
        }
        QTextEdit, QPlainTextEdit {
            background: #FFFFFF;
            color: #182230;
            border: 1px solid #C5CFDA;
            border-radius: 6px;
            padding: 8px;
            selection-background-color: #0B766E;
            selection-color: #FFFFFF;
        }
        QLineEdit:focus, QComboBox:focus, QTextEdit:focus, QPlainTextEdit:focus,
        QSpinBox:focus, QDateEdit:focus, QTimeEdit:focus {
            border: 1px solid #0B766E;
        }
        QLineEdit:disabled, QComboBox:disabled, QTextEdit:disabled {
            background: #F2F4F7;
            color: #98A2B3;
        }
        QComboBox::drop-down {
            subcontrol-origin: padding;
            subcontrol-position: top right;
            width: 30px;
            border: 0;
            border-left: 1px solid #DDE3EA;
            background: #F7F9FB;
            border-top-right-radius: 5px;
            border-bottom-right-radius: 5px;
        }
        QComboBox::drop-down:hover { background: #EEF2F6; }
        QComboBox QAbstractItemView {
            background: #FFFFFF;
            color: #182230;
            border: 1px solid #C9D2DD;
            selection-background-color: #E8F5F3;
            selection-color: #075F59;
            outline: 0;
        }

        QListWidget, QTableWidget {
            background: #FFFFFF;
            alternate-background-color: #F9FAFB;
            color: #182230;
            border: 1px solid #DCE3EA;
            border-radius: 6px;
            outline: 0;
        }
        QListWidget::item { padding: 7px 8px; border-bottom: 1px solid #F0F2F5; }
        QListWidget::item:hover { background: #F7F9FB; }
        QListWidget::item:selected { background: #E8F5F3; color: #075F59; }
        QListWidget#douyinLocationResults {
            background: #FFFFFF;
            border: 1px solid #DCE5E8;
            border-radius: 10px;
            padding: 4px;
            outline: none;
        }
        QListWidget#douyinLocationResults::item {
            min-height: 82px;
            padding: 0;
            border-bottom: 1px solid #EEF2F3;
            border-radius: 7px;
        }
        QListWidget#douyinLocationResults::item:hover {
            background: #F4F9F8;
        }
        QListWidget#douyinLocationResults::item:selected {
            background: #E1F3EF;
            color: #075F59;
        }
        QLabel#douyinLocationResultsTitle {
            color: #536273;
            font-size: 12px;
            font-weight: 600;
            padding: 5px 2px 0;
        }
        QLabel#douyinLocationName {
            color: #182230;
            font-size: 14px;
            font-weight: 700;
        }
        QLabel#douyinLocationAddress {
            color: #667085;
            font-size: 12px;
        }
        QLabel#douyinLocationDistance {
            color: #0B766E;
            background: #E8F5F3;
            border-radius: 5px;
            padding: 2px 6px;
            font-size: 11px;
            font-weight: 600;
        }
        QListWidget#accountTargetList::item {
            min-height: 34px;
            padding: 3px 7px;
        }
        QListWidget#mediaSelectionList::item {
            min-height: 56px;
            padding: 5px 7px;
        }
        QListWidget#platformNav {
            background: #F7F9FB;
            border-color: #E7EBF0;
        }
        QListWidget#platformNav::item {
            padding: 6px 10px;
            border: 0;
            border-left: 3px solid transparent;
        }
        QListWidget#platformNav::item:hover {
            background: #EEF2F6;
        }
        QListWidget#platformNav::item:selected {
            background: #FFFFFF;
            color: #172033;
            border-left: 3px solid #0B766E;
        }
        QTableWidget { gridline-color: #EAECF0; }
        QTableWidget#dataTable { border-radius: 6px; }
        QTableWidget::item { padding: 5px 8px; }
        QTableWidget::item:selected { background: #E8F5F3; color: #172033; }
        QHeaderView::section {
            min-height: 36px;
            background: #F7F9FB;
            color: #475467;
            border: 0;
            border-right: 1px solid #EAECF0;
            border-bottom: 1px solid #DDE3EA;
            padding: 0 8px;
            font-weight: 600;
        }

        QTabWidget, QTabBar { background: #F2F5F8; }
        QTabWidget::pane {
            border: 1px solid #DDE3EA;
            border-radius: 6px;
            background: #FFFFFF;
            top: -1px;
        }
        QTabBar::tab {
            min-height: 36px;
            padding: 0 15px;
            background: #F2F5F8;
            color: #667085;
            border: 0;
            border-bottom: 2px solid transparent;
        }
        QTabBar::tab:hover { color: #344054; background: #F7F9FB; }
        QTabBar::tab:selected {
            color: #075F59;
            background: #FFFFFF;
            border-bottom: 2px solid #0B766E;
            font-weight: 700;
        }
        QTabWidget#mainTabs { background: #F2F5F8; }
        QTabWidget#mainTabs::pane { border: 0; border-radius: 0; background: #F2F5F8; }
        QTabWidget#mainTabs QTabBar::tab { min-width: 108px; min-height: 42px; }
        QStackedWidget#contentStack {
            background: #F8FAFB;
            border: 1px solid #DCE3EA;
            border-radius: 8px;
        }
        QTabBar#contentTabBar {
            background: #E8EEF2;
            border: 1px solid #CDD7E1;
            border-radius: 9px;
        }
        QTabBar#contentTabBar::tab {
            min-width: 88px;
            min-height: 34px;
            margin: 3px 2px;
            padding: 0 12px;
            background: transparent;
            color: #475467;
            border: 1px solid transparent;
            border-radius: 6px;
            font-weight: 600;
        }
        QTabBar#contentTabBar::tab:hover {
            background: #FFFFFF;
            color: #075F59;
        }
        QTabBar#contentTabBar::tab:selected {
            background: #0B766E;
            color: #FFFFFF;
            border: 1px solid #075F59;
            font-weight: 700;
        }
        QTabBar#contentTabBar::tab:selected:focus {
            border: 1px solid #5EEAD4;
        }

        /* 抖音带货：任务型桌面工作台。进度、当前动作和平台回读分层展示，
           不把待完成字段复制成常驻信息侧栏。 */
        QFrame#douyinCommerceCommandBar {
            background: #FFFFFF;
            border: 1px solid #DDE6EB;
            border-left: 4px solid #0B766E;
            border-radius: 10px;
        }
        QLabel#douyinCommerceEyebrow {
            color: #0B766E;
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 1px;
        }
        QLabel#douyinCommerceHeroDetail {
            color: #667085;
            font-size: 12px;
        }
        QLabel#douyinCommerceSessionHint {
            color: #667085;
            font-size: 11px;
        }
        QFrame#douyinCommerceProgress {
            background: transparent;
            border: 0;
        }
        QLabel#douyinCommerceStep {
            background: #FFFFFF;
            color: #667085;
            border: 1px solid #E3E9EE;
            border-radius: 8px;
            padding: 7px 10px;
            font-size: 12px;
            font-weight: 600;
        }
        QLabel#douyinCommerceStep[stepState="active"] {
            background: #0B766E;
            color: #FFFFFF;
            border-color: #0B766E;
            font-weight: 700;
        }
        QLabel#douyinCommerceStep[stepState="complete"] {
            background: #EFF9F6;
            color: #08745C;
            border-color: #B7E5D7;
        }
        QLabel#douyinCommerceStep[stepState="pending"] {
            background: #FFFFFF;
            color: #98A2B3;
            border-color: #E4E9EE;
        }
        QLabel#douyinCommerceProgressContext {
            color: #526174;
            font-size: 12px;
            padding: 2px 3px 0 3px;
        }
        QLabel#douyinCommerceBoundaryLine {
            color: #526174;
            background: #F8FAFC;
            border: 1px solid #E2E8F0;
            border-radius: 8px;
            padding: 9px 12px;
            font-size: 12px;
        }
        QFrame[subPanel="true"][douyinCommerceWorkCard="true"] {
            background: #FFFFFF;
            border: 1px solid #DDE6EB;
            border-radius: 12px;
        }
        QFrame#douyinCommerceReviewPanel {
            background: #FFFFFF;
            border: 1px solid #D7E2EC;
            border-radius: 16px;
        }
        QLabel#douyinCommerceReviewSummarySection {
            color: #1F4E72;
            background: #EEF7FF;
            border: 1px solid #D3E8F8;
            border-radius: 8px;
            padding: 6px 10px;
            font-size: 12px;
            font-weight: 750;
        }
        QFrame#douyinCommerceReviewSummaryItem {
            background: #F8FAFC;
            border: 1px solid #E2E8F0;
            border-radius: 10px;
        }
        QLabel#douyinCommerceReviewSummaryLabel {
            color: #718096;
            background: transparent;
            border: 0;
            font-size: 11px;
            font-weight: 700;
        }
        QLabel#douyinCommerceReviewSummaryValue {
            color: #1D2939;
            background: transparent;
            border: 0;
            font-size: 13px;
            font-weight: 650;
        }
        QLabel#douyinCommerceReviewValidation {
            color: #657D91;
            background: transparent;
            border: 0;
            border-radius: 0;
            padding: 0;
            font-size: 13px;
            font-weight: 500;
        }
        QFrame#douyinCommerceContentAccountColumn,
        QFrame#douyinCommerceContentBodyColumn,
        QFrame#douyinCommerceContentVideoColumn,
        QFrame#douyinCommerceExecutionLog {
            background: #FFFFFF;
            border: 1px solid #DDE6EB;
            border-radius: 12px;
        }
        QFrame#douyinCommercePlatformWorkspace,
        QFrame#douyinCommercePlatformLeftColumn,
        QFrame#douyinCommercePlatformRightColumn,
        QFrame#douyinCommerceReviewWorkspace {
            background: transparent;
            border: 0;
        }
        QFrame#douyinCommercePlatformProgress {
            background: #F5FBFA;
            border: 1px solid #CDE9E3;
            border-radius: 10px;
        }
        QLabel#douyinCommercePlatformProgressText {
            color: #0B766E;
            font-size: 12px;
            font-weight: 700;
        }
        QFrame#douyinCommercePlatformReviewDock {
            background: #FFFFFF;
            border: 1px solid #C8E5DF;
            border-top: 3px solid #0B766E;
            border-radius: 10px;
        }
        QLabel#douyinCommercePlatformReviewStatus {
            color: #526174;
            font-size: 12px;
            font-weight: 600;
        }
        QLabel[douyinCommerceSelectionCard="true"] {
            color: #344054;
            background: #F8FAFB;
            border: 1px solid #DCE3EA;
            border-radius: 8px;
            padding: 11px 13px;
        }
        QLabel#douyinCommerceContentSaveStatus {
            color: #526174;
            background: #F0F7F6;
            border-left: 3px solid #89B9B3;
            border-radius: 6px;
            padding: 8px 10px;
            font-size: 11px;
        }
        QFrame[subPanel="true"][douyinCommerceWorkCard="true"][workState="active"] {
            border: 2px solid #8ED3CB;
            background: #FFFFFF;
        }
        QFrame[subPanel="true"][douyinCommerceWorkCard="true"][workState="complete"] {
            border-left: 4px solid #0B766E;
            background: #FCFEFD;
        }
        QFrame#douyinCommerceOperationDock {
            background: #FFFFFF;
            border: 1px solid #C8E5DF;
            border-top: 3px solid #0B766E;
            border-radius: 10px;
        }
        QLabel#douyinCommerceOperationTitle {
            color: #172033;
            font-size: 13px;
            font-weight: 700;
        }
        QLabel#douyinCommerceOperationDetail {
            color: #526174;
            font-size: 12px;
        }
        QLabel#douyinCommerceOperationStatus {
            color: #667085;
            background: #F2F4F7;
            border-radius: 6px;
            padding: 4px 8px;
            font-size: 11px;
            font-weight: 700;
        }
        QLabel#douyinCommerceOperationStatus[operationState="ready"] {
            color: #08745C;
            background: #ECFDF3;
        }
        QLabel#douyinCommerceOperationStatus[operationState="warning"] {
            color: #B54708;
            background: #FFFAEB;
        }
        QLabel#douyinCommerceOperationStatus[operationState="running"] {
            color: #175CD3;
            background: #EFF8FF;
        }
        QFrame#douyinCommerceUploadProgress {
            background: #F8FAFC;
            border: 1px solid #DCE7EF;
            border-radius: 8px;
        }
        QLabel#douyinCommerceUploadProgressLabel {
            color: #344054;
            font-size: 11px;
            font-weight: 700;
        }
        QProgressBar#douyinCommerceUploadProgressBar {
            background: #E7EEF3;
            border: 0;
            border-radius: 3px;
            min-height: 6px;
            max-height: 6px;
        }
        QProgressBar#douyinCommerceUploadProgressBar::chunk {
            background: #0B8A80;
            border-radius: 3px;
        }
        QFrame#douyinCommercePlatformCollectorProgress {
            background: #F8FAFC;
            border: 1px solid #DCE7EF;
            border-radius: 8px;
        }
        QLabel#douyinCommercePlatformCollectorProgressLabel {
            color: #344054;
            font-size: 11px;
            font-weight: 700;
        }
        QProgressBar#douyinCommercePlatformCollectorProgressBar {
            background: #E7EEF3;
            border: 0;
            border-radius: 3px;
            min-height: 6px;
            max-height: 6px;
        }
        QProgressBar#douyinCommercePlatformCollectorProgressBar::chunk {
            background: #0B8A80;
            border-radius: 3px;
        }
        QFrame#douyinCommerceLoginRequired {
            background: #FFFAEB;
            border: 1px solid #FEDF89;
            border-radius: 8px;
        }
        QLabel#douyinCommerceLoginRequiredText {
            color: #7A2E0E;
            font-size: 12px;
            font-weight: 700;
        }
        QFrame#douyinCommerceThreeColumn {
            background: transparent;
            border: 0;
        }
        QFrame#douyinCommerceTagHost,
        QFrame#douyinCommerceTagHistoryHost {
            background: #F8FAFB;
            border: 1px solid #E2E8EE;
            border-radius: 8px;
        }
        QPushButton#douyinCommerceTagChip {
            background: #E8F5F3;
            color: #075F59;
            border: 1px solid #B7E5D7;
            border-radius: 12px;
            padding: 3px 8px;
            font-size: 11px;
            font-weight: 600;
        }
        QPushButton#douyinCommerceTagChip:hover {
            background: #D7EEE9;
            border-color: #7FC9BB;
        }
        QFrame#douyinCommerceTagHistoryChip {
            background: #FFFFFF;
            border: 1px solid #D8E1E8;
            border-radius: 12px;
        }
        QPushButton#douyinCommerceTagHistoryAdd {
            color: #526174;
            background: transparent;
            border: 0;
            padding: 2px 1px;
            font-size: 11px;
        }
        QPushButton#douyinCommerceTagHistoryAdd:hover {
            color: #075F59;
            background: transparent;
        }
        QPushButton#douyinCommerceTagHistoryRemove {
            min-width: 14px;
            max-width: 14px;
            min-height: 14px;
            max-height: 14px;
            color: #98A6B5;
            background: transparent;
            border: 0;
            border-radius: 7px;
            padding: 0;
            font-size: 14px;
            font-weight: 700;
        }
        QPushButton#douyinCommerceTagHistoryRemove:hover {
            color: #FFFFFF;
            background: #E05A47;
        }
        QListWidget#douyinCommerceMusicCandidates,
        QListWidget#douyinCommerceLocationResults {
            background: #FFFFFF;
            border: 1px solid #D7E2E8;
            border-radius: 10px;
            padding: 3px;
        }
        QListWidget#douyinCommerceMusicCandidates::item,
        QListWidget#douyinCommerceLocationResults::item {
            color: #223249;
            border-bottom: 1px solid #EEF2F5;
            border-radius: 7px;
            padding: 10px 11px;
        }
        QListWidget#douyinCommerceMusicCandidates::item:hover,
        QListWidget#douyinCommerceLocationResults::item:hover {
            background: #F4F9F8;
        }
        QListWidget#douyinCommerceMusicCandidates::item:selected,
        QListWidget#douyinCommerceLocationResults::item:selected {
            color: #075F59;
            background: #E1F3EF;
        }
        QFrame#douyinCommercePlatformActionDock {
            background: #FFFFFF;
            border: 1px solid #C8E5DF;
            border-top: 3px solid #0B766E;
            border-radius: 10px;
        }
        QLabel#douyinCommerceReadbackValue {
            color: #344054;
            font-size: 11px;
            line-height: 1.35;
        }
        QFrame#douyinCommerceWorkspace {
            background: transparent;
            border: 0;
        }
        QSplitter#douyinCommerceWorkspace::handle {
            background: transparent;
            width: 10px;
        }
        QSplitter#douyinCommerceWorkspace::handle:hover {
            background: #DDEBE8;
            border-radius: 4px;
        }
        QScrollArea#douyinCommerceSummaryRail {
            background: transparent;
            border: 0;
        }
        QFrame#douyinCommerceSummaryRailContent {
            background: #FFFFFF;
            border: 1px solid #DDE6EB;
            border-radius: 14px;
        }
        QLabel#douyinCommerceRailTitle {
            color: #172033;
            font-size: 16px;
            font-weight: 700;
        }
        QLabel#douyinCommerceRailSubtitle {
            color: #667085;
            font-size: 11px;
            line-height: 1.4;
        }
        QFrame#douyinCommerceActionCard {
            background: #EEF8F6;
            border: 1px solid #C8E8E1;
            border-radius: 10px;
        }
        QLabel#douyinCommerceActionTitle {
            color: #087266;
            font-size: 11px;
            font-weight: 700;
        }
        QLabel#douyinCommerceActionValue {
            color: #173B3A;
            font-size: 12px;
            font-weight: 600;
        }
        QFrame#douyinCommerceSummaryRow {
            background: transparent;
            border: 0;
            border-bottom: 1px solid #EDF1F3;
        }
        QLabel#douyinCommerceSummaryLabel {
            color: #8A96A3;
            font-size: 11px;
            font-weight: 600;
        }
        QLabel#douyinCommerceSummaryValue {
            color: #25364A;
            font-size: 12px;
            font-weight: 600;
        }
        QLabel#douyinCommerceBoundary {
            color: #5D6878;
            background: #F7F9FA;
            border: 1px solid #E4EAEE;
            border-radius: 8px;
            padding: 10px;
            font-size: 11px;
            line-height: 1.4;
        }
        QWidget#douyinCommerceStepBody {
            background: transparent;
        }
        QScrollArea#douyinCommerceStepScroll {
            background: transparent;
        }
        QFrame[subPanel="true"][douyinCommerceCard="true"] {
            background: #FFFFFF;
            border: 1px solid #DDE6EB;
            border-radius: 12px;
        }
        QLabel#douyinCommerceInlineNotice,
        QLabel#douyinCommerceLocationStatus {
            color: #526174;
            background: #F5F8FA;
            border: 1px solid #E4EAEE;
            border-radius: 8px;
            padding: 9px 11px;
            font-size: 12px;
        }
        QLabel#douyinCommerceStageError {
            color: #9A3412;
            background: #FFF7ED;
            border: 1px solid #FED7AA;
            border-left: 3px solid #EA580C;
            border-radius: 8px;
            padding: 9px 11px;
            font-size: 12px;
            line-height: 1.45;
        }
        QLabel#douyinCommerceLocationConfirmedCard {
            color: #075F59;
            background: #EFF9F6;
            border: 1px solid #B7E5D7;
            border-radius: 10px;
            padding: 12px;
        }
        QLabel#douyinCommercePublishModeHint {
            color: #526174;
            background: #F8FAFB;
            border-left: 3px solid #89B9B3;
            padding: 7px 10px;
            font-size: 12px;
        }

        /* 抖音带货内容准备：用户验收后的三栏桌面稿。
           仅覆盖内容准备页；上传后的受控平台设置页仍沿用原有状态样式。 */
        QFrame#douyinCommerceReferenceHeader {
            background: transparent;
            border: 0;
        }
        QLabel#douyinCommerceReferenceTitle {
            color: #172033;
            font-size: 24px;
            font-weight: 750;
        }
        QLabel#douyinCommerceReferenceSubtitle {
            color: #718096;
            font-size: 15px;
        }
        QLabel#douyinCommerceReferenceSaveBadge {
            min-width: 96px;
            color: #667085;
            background: #FFFFFF;
            border: 1px solid #D9E3EE;
            border-radius: 9px;
            padding: 8px 12px;
            font-size: 12px;
            font-weight: 700;
        }
        QLabel#douyinCommerceReferenceSaveBadge[saveState="saved"] {
            color: #0A867F;
            background: #EFFBF8;
            border-color: #C6E8E2;
        }
        QLabel#douyinCommerceReferenceSaveBadge[saveState="session"] {
            color: #1769AA;
            background: #EFF7FE;
            border-color: #C9E2F4;
        }
        QLabel#douyinCommerceReferenceSaveBadge[saveState="busy"] {
            color: #A06A00;
            background: #FFF7E7;
            border-color: #F2D69B;
        }
        QFrame#douyinCommerceProgress {
            background: transparent;
            border: 0;
        }
        QFrame#douyinCommerceStepCard {
            background: #FFFFFF;
            border: 1px solid #DCE6F0;
            border-radius: 10px;
        }
        QFrame#douyinCommerceStepCard[stepState="active"] {
            background: #EDF7FF;
            border: 2px solid #AED8F3;
        }
        QFrame#douyinCommerceStepCard[stepState="complete"] {
            background: #F0FBF8;
            border-color: #BEE6DC;
        }
        QFrame#douyinCommerceStepCard[stepState="pending"] {
            background: #FFFFFF;
            border-color: #DCE6F0;
        }
        QLabel#douyinCommerceStepNumber {
            min-width: 26px;
            max-width: 26px;
            min-height: 26px;
            max-height: 26px;
            color: #8FA0B4;
            background: #EFF4F8;
            border: 0;
            border-radius: 13px;
            font-size: 12px;
            font-weight: 750;
        }
        QFrame#douyinCommerceStepCard[stepState="active"] QLabel#douyinCommerceStepNumber {
            color: #FFFFFF;
            background: #1C78B7;
        }
        QFrame#douyinCommerceStepCard[stepState="complete"] QLabel#douyinCommerceStepNumber {
            color: #0A867F;
            background: #DDF5EE;
        }
        QLabel#douyinCommerceStep {
            color: #91A0B4;
            background: transparent;
            border: 0;
            padding: 0;
            font-size: 14px;
            font-weight: 700;
        }
        QLabel#douyinCommerceStep[stepState="active"] {
            color: #1D70AF;
            background: transparent;
            border: 0;
            padding: 0;
        }
        QLabel#douyinCommerceStep[stepState="complete"] {
            color: #0A867F;
            background: transparent;
            border: 0;
            padding: 0;
        }
        QLabel#douyinCommerceStep[stepState="pending"] {
            color: #91A0B4;
            background: transparent;
            border: 0;
            padding: 0;
        }
        QFrame#douyinCommerceStepCard[stepState="active"] QLabel#douyinCommerceStep {
            color: #1D70AF;
        }
        QFrame#douyinCommerceStepCard[stepState="complete"] QLabel#douyinCommerceStep {
            color: #0A867F;
        }
        QLabel#douyinCommerceProgressContext {
            color: #75869A;
            background: transparent;
            border: 0;
            padding: 1px 2px 3px 2px;
            font-size: 13px;
        }
        QFrame#douyinCommerceStageSwitcher {
            background: transparent;
            border: 0;
        }
        QPushButton#douyinCommerceStageButton {
            min-height: 34px;
            color: #6F8196;
            background: #FFFFFF;
            border: 1px solid #D5E0EA;
            border-radius: 8px;
            padding: 0 15px;
            font-size: 13px;
            font-weight: 700;
        }
        QPushButton#douyinCommerceStageButton[variant="primary"] {
            color: #176DAA;
            background: #EEF7FF;
            border-color: #ABD8F3;
        }
        QPushButton#douyinCommerceStageButton:disabled {
            color: #AFBAC6;
            background: #F8FAFC;
            border-color: #E5EBF0;
        }
        QFrame#douyinCommerceContentAccountColumn,
        QFrame#douyinCommerceContentBodyColumn,
        QFrame#douyinCommerceContentVideoColumn,
        QFrame#douyinCommerceExecutionLog {
            background: #FFFFFF;
            border: 1px solid #D7E2EC;
            border-radius: 16px;
        }
        QLabel#douyinCommerceExecutionLogTitle {
            color: #22334A;
            font-size: 16px;
            font-weight: 750;
        }
        QLabel#douyinCommerceExecutionLogHint {
            color: #087C78;
            background: #E8F7F5;
            border: 1px solid #BCE8E1;
            border-radius: 8px;
            padding: 2px 7px;
            font-size: 11px;
            font-weight: 700;
        }
        QListWidget#douyinCommerceMusicCandidates {
            color: #243B53;
            background: #F8FBFC;
            border: 1px solid #BFD8E6;
            border-radius: 8px;
            padding: 3px;
            outline: 0;
        }
        QListWidget#douyinCommerceMusicCandidates::item {
            min-height: 30px;
            border-radius: 5px;
            padding: 4px 7px;
        }
        QListWidget#douyinCommerceMusicCandidates::item:hover,
        QListWidget#douyinCommerceMusicCandidates::item:selected {
            color: #075F59;
            background: #E8F7F5;
        }
        QPushButton#douyinCommerceCopyExecutionLog {
            min-height: 26px;
            color: #167A9F;
            background: #F4FAFC;
            border: 1px solid #C7E2EE;
            border-radius: 7px;
            padding: 0 8px;
            font-size: 11px;
            font-weight: 700;
        }
        QPushButton#douyinCommerceCopyExecutionLog:hover {
            color: #075F59;
            background: #E8F7F5;
            border-color: #9ED8CF;
        }
        QPushButton#douyinCommerceClearExecutionLog {
            min-height: 26px;
            color: #8A5B35;
            background: #FFF8EF;
            border: 1px solid #F2D7B3;
            border-radius: 7px;
            padding: 1px 8px;
            font-size: 11px;
            font-weight: 700;
        }
        QPushButton#douyinCommerceClearExecutionLog:hover {
            color: #7A2E0E;
            background: #FFF0DB;
            border-color: #EAB977;
        }
        QPlainTextEdit#douyinCommerceExecutionLogOutput {
            color: #C7E9E3;
            background: #17243A;
            border: 1px solid #29415E;
            border-radius: 10px;
            padding: 9px;
            font-family: "Cascadia Mono", "Consolas", monospace;
            font-size: 13px;
            selection-background-color: #315C6B;
        }
        QFrame#douyinCommerceReferenceCardHead {
            background: transparent;
            border: 0;
            border-bottom: 1px solid #E6EDF3;
        }
        QLabel#douyinCommerceReferenceCardEyebrow {
            color: #22334A;
            font-size: 20px;
            font-weight: 750;
        }
        QLabel#douyinCommerceReferenceCardTitle {
            color: #22334A;
            font-size: 20px;
            font-weight: 750;
        }
        QLabel#douyinCommerceFieldLabel {
            color: #64768C;
            font-size: 13px;
            font-weight: 700;
        }
        QLabel#douyinCommerceFieldHint {
            color: #96A4B4;
            font-size: 11px;
        }
        QFrame#douyinCommerceAccountIdentityCard {
            background: #F4FCFA;
            border: 1px solid #BDE8E0;
            border-radius: 13px;
        }
        QLabel#douyinCommerceAccountAvatar {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 #2B668B, stop:0.55 #73A7B6, stop:1 #C1E7E1);
            border: 0;
            border-radius: 14px;
        }
        QLabel#douyinCommerceAccountIdentityName {
            color: #20344B;
            background: transparent;
            border: 0;
            font-size: 18px;
            font-weight: 750;
        }
        QLabel#douyinCommerceAccountCard {
            color: #668197;
            background: transparent;
            border: 0;
            padding: 0;
            font-size: 12px;
        }
        QComboBox#douyinCommerceAccount {
            min-height: 42px;
            color: #20344B;
            background: #F9FCFD;
            border: 1px solid #C7D9E6;
            border-radius: 10px;
            padding: 4px 42px 4px 12px;
            font-size: 14px;
            font-weight: 700;
        }
        QComboBox#douyinCommerceAccount:hover,
        QComboBox#douyinCommerceAccount:focus {
            background: #FFFFFF;
            border-color: #64B5C6;
        }
        QComboBox#douyinCommerceAccount::drop-down {
            width: 34px;
            background: #F0F8FA;
            border: 0;
            border-left: 1px solid #D7E8EC;
            border-top-right-radius: 9px;
            border-bottom-right-radius: 9px;
        }
        QComboBox#douyinCommerceAccount::drop-down:hover {
            background: #E4F4F3;
        }
        QComboBox#douyinCommerceAccount QAbstractItemView {
            color: #20344B;
            background: #FFFFFF;
            border: 1px solid #BFD4DF;
            border-radius: 10px;
            padding: 5px;
            outline: 0;
            selection-background-color: #E8F7F5;
            selection-color: #087C78;
        }
        QLabel#douyinCommerceContentSaveStatus {
            color: #5C7587;
            background: #F2FBF9;
            border: 1px solid #D2ECE6;
            border-left: 3px solid #79C1B5;
            border-radius: 8px;
            padding: 9px 10px;
            font-size: 11px;
        }
        QFrame#douyinCommerceVideoPreview {
            background: #F0F4F8;
            border: 1px solid #E0E8EF;
            border-radius: 13px;
        }
        QLabel#douyinCommerceVideoThumbnail {
            color: #FFFFFF;
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 #213248, stop:0.6 #4D6B85, stop:1 #7895AA);
            border: 0;
            border-radius: 12px;
            font-size: 13px;
            font-weight: 700;
        }
        QLabel#douyinCommerceVideoThumbnail[hasPreview="true"] {
            color: transparent;
            background: #E7EEF4;
        }
        QLabel#douyinCommerceVideoCard {
            color: #31455E;
            background: transparent;
            border: 0;
            padding: 0;
            font-size: 14px;
            font-weight: 650;
            line-height: 1.45;
        }
        QFrame#douyinCommerceVideoReplace {
            background: #FFFFFF;
            border: 1px dashed #B8CBDC;
            border-radius: 10px;
        }
        QPushButton#douyinCommerceVideoReplaceButton {
            min-height: 34px;
            color: #59778D;
            background: transparent;
            border: 0;
            font-size: 13px;
            font-weight: 700;
        }
        QPushButton#douyinCommerceVideoReplaceButton:hover {
            color: #167A9F;
            background: #F4FAFC;
        }
        QMenu#douyinCommerceVideoPickerMenu {
            background: #FFFFFF;
            border: 1px solid #C9D9E5;
            border-radius: 12px;
            padding: 6px;
        }
        QScrollArea#douyinCommerceVideoPickerScroll,
        QWidget#douyinCommerceVideoPickerRows {
            background: #FFFFFF;
            border: 0;
        }
        QPushButton#douyinCommerceVideoPickerItem {
            color: #23384F;
            background: #FFFFFF;
            border: 1px solid transparent;
            border-radius: 9px;
            padding: 5px 10px;
            text-align: left;
            font-size: 13px;
            font-weight: 700;
        }
        QPushButton#douyinCommerceVideoPickerItem:hover {
            color: #0B716E;
            background: #F2FBF9;
            border-color: #C7E9E3;
        }
        QPushButton#douyinCommerceVideoPickerItem:checked {
            color: #087C78;
            background: #E8F7F5;
            border-color: #94D7CA;
        }
        QLabel#douyinCommerceVideoNotice {
            color: #936812;
            background: #FFF7E6;
            border: 0;
            border-radius: 10px;
            padding: 11px 12px;
            font-size: 12px;
            font-weight: 650;
        }
        QFrame#douyinCommerceReferenceFooter,
        QFrame#douyinCommercePlatformReviewDock {
            background: #FBFFFE;
            border: 1px solid #C7E8E1;
            border-top: 3px solid #0B8A85;
            border-radius: 14px;
        }
        QFrame#douyinCommerceReferenceFooter QLabel#douyinCommerceOperationTitle,
        QFrame#douyinCommerceReferenceFooter QLabel#douyinCommerceOperationDetail,
        QFrame#douyinCommerceReferenceFooter QLabel#douyinCommerceOperationStatus,
        QFrame#douyinCommerceReferenceFooter QLabel#douyinCommerceReviewValidation,
        QFrame#douyinCommercePlatformReviewDock QLabel#douyinCommercePlatformReviewStatus {
            font-size: 13px;
        }
        QFrame#douyinCommerceReferenceFooter QPushButton[footerAction="true"],
        QFrame#douyinCommercePlatformReviewDock QPushButton[footerAction="true"] {
            min-height: 46px;
            max-height: 46px;
            min-width: 178px;
            padding: 0 18px;
            font-size: 15px;
            font-weight: 750;
        }
        QFrame#douyinCommerceBatchItemRow {
            background: #F7FBFD;
            border: 1px solid #E0EBF1;
            border-radius: 7px;
        }
        QComboBox#douyinCommerceBatchLocationCandidates {
            /* Qt 样式表高度作用于内容区；加上上下内边距和边框后总高正好 30px。 */
            min-height: 24px;
            max-height: 24px;
            color: #263B50;
            background: #FFFFFF;
            border: 1px solid #C9DAE5;
            border-radius: 7px;
            padding: 2px 30px 2px 9px;
        }
        QComboBox#douyinCommerceBatchLocationCandidates:hover,
        QComboBox#douyinCommerceBatchLocationCandidates:focus {
            border-color: #74B9C6;
        }
        QFrame#douyinCommerceBatchReviewRow {
            background: #F4FCFA;
            border: 1px solid #CBEAE4;
            border-radius: 9px;
        }
        QLabel#douyinCommerceOperationTitle {
            color: #29465C;
            font-size: 16px;
            font-weight: 750;
        }
        QLabel#douyinCommerceOperationDetail {
            color: #657D91;
            font-size: 13px;
        }
        QLabel#douyinCommerceOperationStatus {
            color: #607286;
            background: #EEF4F7;
            border: 0;
            border-radius: 8px;
            padding: 7px 10px;
            font-size: 12px;
            font-weight: 700;
        }
        QPushButton#douyinCommerceUpload {
            min-height: 46px;
            min-width: 178px;
            color: #FFFFFF;
            background: #0B8A85;
            border: 1px solid #0A7773;
            border-radius: 10px;
            padding: 0 20px;
            font-size: 15px;
            font-weight: 750;
        }
        QPushButton#douyinCommerceUpload:hover {
            background: #087A76;
        }
        QPushButton#douyinCommerceUpload:disabled {
            color: #8E9CAA;
            background: #EDF2F5;
            border-color: #E0E7EB;
        }
        QCheckBox { spacing: 8px; color: #344054; min-height: 28px; }
        QCheckBox::indicator, QListWidget::indicator {
            width: 18px;
            height: 18px;
            border: 2px solid #667085;
            border-radius: 5px;
            background: #FFFFFF;
        }
        QCheckBox::indicator:hover, QListWidget::indicator:hover {
            border-color: #0B766E;
            background: #F0FDFA;
        }
        QCheckBox::indicator:checked, QListWidget::indicator:checked {
            border-color: #0F766E;
            background: #0F766E;
        }
        QCheckBox::indicator:disabled, QListWidget::indicator:disabled {
            border-color: #D0D5DD;
            background: #EAECF0;
        }
        QProgressBar {
            min-height: 18px;
            border: 1px solid #DDE3EA;
            border-radius: 5px;
            background: #EEF2F6;
            color: #344054;
            text-align: center;
            font-size: 11px;
        }
        QProgressBar::chunk { background: #0B766E; border-radius: 4px; }
        QSplitter::handle { background: transparent; width: 8px; }
        QSplitter::handle:hover { background: #DDE3EA; }
        QScrollArea { border: 0; background: transparent; }
        QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
        QScrollBar::handle:vertical { background: #C9D2DD; border-radius: 4px; min-height: 28px; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
        QScrollBar::handle:horizontal { background: #C9D2DD; border-radius: 4px; min-width: 28px; }
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
        QMenu {
            background: #FFFFFF;
            color: #182230;
            border: 1px solid #DDE3EA;
            padding: 5px;
        }
        QMenu::item { padding: 7px 26px 7px 10px; border-radius: 4px; }
        QMenu::item:selected { background: #E8F5F3; color: #075F59; }
        QTextEdit#executionLog {
            background: #162033;
            color: #DCE5EF;
            border: 1px solid #25344C;
            font-family: "SFMono-Regular", "Cascadia Mono", "Consolas";
            font-size: 13px;
        }
        QPlainTextEdit#diagnosticOutput {
            background: #162033;
            color: #DCE5EF;
            border: 1px solid #25344C;
            border-radius: 6px;
            padding: 12px;
            font-family: "SFMono-Regular", "Cascadia Mono", "Consolas";
            font-size: 12px;
            selection-background-color: #0B766E;
        }
        QLabel#emptyStateTitle {
            color: #344054;
            font-size: 14px;
            font-weight: 600;
        }
        QLabel#emptyStateDetail {
            color: #98A2B3;
            font-size: 12px;
        }
        QToolTip { background: #101828; color: #FFFFFF; border: 0; padding: 5px; }
        """
    )
    checkmark_path = (ROOT_DIR / "ui" / "assets" / "checkbox-check.svg").as_posix()
    chevron_path = (ROOT_DIR / "ui" / "assets" / "chevron-down.svg").as_posix()
    app.setStyleSheet(
        app.styleSheet()
        + f"""
        QCheckBox::indicator:checked, QListWidget::indicator:checked {{
            image: url("{checkmark_path}");
        }}
        QComboBox::down-arrow {{
            image: url("{chevron_path}");
            width: 16px;
            height: 16px;
        }}
        """
    )
