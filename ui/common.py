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
            min-width: 108px;
            min-height: 34px;
            margin: 3px;
            padding: 0 16px;
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
            border: 2px solid #5EEAD4;
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
            font-size: 11px;
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
