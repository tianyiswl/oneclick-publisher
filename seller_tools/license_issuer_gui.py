# -*- coding: utf-8 -*-
"""一键发卖家激活码管理器。"""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtCore import QDate, Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDateEdit,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from seller_tools.license_crypto import issue_activation_code, private_key_file
from seller_tools.license_history import append_history, load_history


APP_TITLE = "一键发激活码管理器"


def resource_path(relative: str) -> Path:
    root = Path(getattr(sys, "_MEIPASS", ROOT_DIR))
    return root / relative


def button(text: str, *, primary: bool = False) -> QPushButton:
    result = QPushButton(text)
    result.setProperty("primary", primary)
    result.setCursor(Qt.CursorShape.PointingHandCursor)
    return result


class LicenseIssuerWindow(QMainWindow):
    def __init__(self, *, load_saved_history: bool = True) -> None:
        super().__init__()
        self.records: list[dict] = []
        self.setWindowTitle(APP_TITLE)
        self.resize(1040, 760)
        self.setMinimumSize(900, 680)
        icon_path = resource_path("ui/assets/fashetai-app-icon.png")
        if icon_path.is_file():
            self.setWindowIcon(QIcon(str(icon_path)))

        root = QWidget()
        root.setObjectName("appRoot")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        header = QHBoxLayout()
        header.setSpacing(20)
        title_area = QVBoxLayout()
        title_area.setSpacing(4)
        title = QLabel(APP_TITLE)
        title.setObjectName("pageTitle")
        subtitle = QLabel("输入客户机器码，生成仅限该电脑使用的激活码")
        subtitle.setObjectName("subtitle")
        title_area.addWidget(title)
        title_area.addWidget(subtitle)
        header.addLayout(title_area)
        header.addStretch()
        self.key_status = QLabel()
        self.key_status.setObjectName("statusBadge")
        header.addWidget(self.key_status)
        layout.addLayout(header)

        form_card = QFrame()
        form_card.setObjectName("panel")
        form_layout = QVBoxLayout(form_card)
        form_layout.setContentsMargins(24, 20, 24, 20)
        form_layout.setSpacing(12)
        form_title = QLabel("生成激活码")
        form_title.setObjectName("sectionTitle")
        form_layout.addWidget(form_title)

        form = QGridLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(12)
        form.setColumnMinimumWidth(0, 72)
        form.setColumnMinimumWidth(2, 110)
        form.setColumnMinimumWidth(3, 72)
        form.setColumnMinimumWidth(4, 170)
        form.setColumnStretch(1, 5)
        form.setRowMinimumHeight(0, 46)
        form.setRowMinimumHeight(1, 46)

        machine_label = QLabel("机器码")
        machine_label.setObjectName("fieldLabel")
        self.machine_input = QLineEdit()
        self.machine_input.setPlaceholderText("从客户激活窗口复制 32 位机器码")
        self.machine_input.setClearButtonEnabled(True)
        self.machine_input.setMinimumWidth(280)
        self.machine_input.setFixedHeight(42)
        self.machine_input.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        paste_button = button("粘贴")
        paste_button.setObjectName("compactButton")
        paste_button.setFixedSize(110, 42)
        paste_button.clicked.connect(self.paste_machine_code)
        form.addWidget(machine_label, 0, 0)
        form.addWidget(self.machine_input, 0, 1)
        form.addWidget(paste_button, 0, 2)

        order_label = QLabel("订单备注")
        order_label.setObjectName("fieldLabel")
        self.order_input = QLineEdit()
        self.order_input.setPlaceholderText("淘宝订单号、客户昵称或售后备注")
        self.order_input.setClearButtonEnabled(True)
        self.order_input.setFixedHeight(42)
        form.addWidget(order_label, 1, 0)
        form.addWidget(self.order_input, 1, 1, 1, 2)

        validity_label = QLabel("授权期限")
        validity_label.setObjectName("fieldLabel")
        self.permanent_check = QCheckBox("长期有效")
        self.permanent_check.setChecked(False)
        self.permanent_check.setCursor(Qt.CursorShape.PointingHandCursor)
        expiry_label = QLabel("到期日期")
        expiry_label.setObjectName("fieldLabel")
        self.expiry_input = QDateEdit()
        self.expiry_input.setCalendarPopup(True)
        self.expiry_input.setDisplayFormat("yyyy-MM-dd")
        self.expiry_input.setDate(QDate.currentDate().addMonths(1))
        self.expiry_input.setEnabled(True)
        self.expiry_input.setMinimumWidth(170)
        self.expiry_input.setFixedHeight(42)
        self.expiry_input.setCursor(Qt.CursorShape.PointingHandCursor)
        self.permanent_check.toggled.connect(
            lambda checked: self.expiry_input.setEnabled(not checked)
        )
        form.addWidget(validity_label, 0, 3)
        form.addWidget(self.permanent_check, 0, 4)
        form.addWidget(expiry_label, 1, 3)
        form.addWidget(self.expiry_input, 1, 4)
        form_layout.addLayout(form)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        generate_button = button("生成并复制激活码", primary=True)
        generate_button.clicked.connect(self.generate_code)
        clear_button = button("清空表单")
        clear_button.clicked.connect(self.clear_form)
        actions.addWidget(generate_button)
        actions.addWidget(clear_button)
        actions.addStretch()
        self.operation_status = QLabel("")
        self.operation_status.setObjectName("operationStatus")
        self.operation_status.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        actions.addWidget(self.operation_status)
        form_layout.addLayout(actions)
        layout.addWidget(form_card)

        code_card = QFrame()
        code_card.setObjectName("panel")
        code_layout = QVBoxLayout(code_card)
        code_layout.setContentsMargins(24, 18, 24, 18)
        code_layout.setSpacing(12)
        code_header = QHBoxLayout()
        code_title = QLabel("本次激活码")
        code_title.setObjectName("sectionTitle")
        copy_button = button("复制")
        copy_button.clicked.connect(self.copy_current_code)
        code_header.addWidget(code_title)
        code_header.addStretch()
        code_header.addWidget(copy_button)
        code_layout.addLayout(code_header)
        self.code_output = QPlainTextEdit()
        self.code_output.setReadOnly(True)
        self.code_output.setPlaceholderText("生成后的激活码会显示在这里")
        self.code_output.setFixedHeight(92)
        self.code_output.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        code_layout.addWidget(self.code_output)
        layout.addWidget(code_card)

        history_card = QFrame()
        history_card.setObjectName("panel")
        history_layout = QVBoxLayout(history_card)
        history_layout.setContentsMargins(24, 18, 24, 20)
        history_layout.setSpacing(12)
        history_header = QHBoxLayout()
        history_title = QLabel("最近出码记录")
        history_title.setObjectName("sectionTitle")
        history_hint = QLabel("输入序号可复制对应激活码")
        history_hint.setObjectName("sectionHint")
        self.history_index_input = QLineEdit("1")
        self.history_index_input.setPlaceholderText("序号")
        self.history_index_input.setFixedWidth(64)
        copy_history_button = button("复制指定记录")
        copy_history_button.clicked.connect(self.copy_selected_history)
        history_header.addWidget(history_title)
        history_header.addWidget(history_hint)
        history_header.addStretch()
        history_header.addWidget(self.history_index_input)
        history_header.addWidget(copy_history_button)
        history_layout.addLayout(history_header)

        # macOS 辅助功能会在输入时读取整个窗口树。QTableWidget 动态
        # 重建行时可使 AppKit 读到失效的可访问性数组并原生崩溃，故改用
        # 结构稳定的只读文本。
        self.history_output = QPlainTextEdit()
        self.history_output.setReadOnly(True)
        self.history_output.setPlaceholderText("暂无出码记录")
        self.history_output.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        history_layout.addWidget(self.history_output)
        layout.addWidget(history_card, 1)

        self.apply_style()
        self.refresh_key_status()
        if load_saved_history:
            self.refresh_history()

    def apply_style(self) -> None:
        self.setStyleSheet(
            """
            QWidget {
                color: #172033;
                font-size: 14px;
            }
            QWidget#appRoot {
                background: #f3f6f9;
            }
            QLabel {
                background: transparent;
            }
            QLabel#pageTitle {
                font-size: 26px;
                font-weight: 700;
            }
            QLabel#subtitle {
                color: #64748b;
                font-size: 14px;
            }
            QLabel#sectionTitle {
                color: #172033;
                font-size: 17px;
                font-weight: 700;
            }
            QLabel#sectionHint {
                color: #94a3b8;
                font-size: 12px;
                padding-left: 8px;
            }
            QLabel#fieldLabel {
                color: #475569;
                font-weight: 600;
            }
            QLabel#statusBadge {
                background: #e8f8f3;
                color: #047857;
                border: 1px solid #a7e4cf;
                border-radius: 8px;
                padding: 9px 14px;
                font-weight: 700;
            }
            QLabel#operationStatus {
                color: #047857;
                font-weight: 700;
            }
            QFrame#panel {
                background: #ffffff;
                border: 1px solid #d7e0e9;
                border-radius: 8px;
            }
            QLineEdit, QDateEdit {
                background: #ffffff;
                border: 1px solid #bcc9d6;
                border-radius: 6px;
                min-height: 40px;
                padding: 0 12px;
                selection-background-color: #00897b;
            }
            QLineEdit:focus, QDateEdit:focus {
                border: 2px solid #00897b;
            }
            QLineEdit:disabled, QDateEdit:disabled {
                background: #f1f5f9;
                color: #94a3b8;
                border-color: #d8e0e8;
            }
            QPlainTextEdit {
                background: #f8fafc;
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                padding: 10px 12px;
                selection-background-color: #00897b;
                font-family: "SF Mono", "Menlo", monospace;
            }
            QPlainTextEdit:focus {
                border: 2px solid #00897b;
            }
            QPushButton {
                background: #ffffff;
                border: 1px solid #b8c4d2;
                border-radius: 6px;
                min-height: 38px;
                padding: 0 18px;
                font-weight: 600;
            }
            QPushButton#compactButton {
                min-width: 96px;
            }
            QPushButton:hover {
                border-color: #00897b;
                color: #00796b;
            }
            QPushButton:pressed {
                background: #eef7f5;
            }
            QPushButton[primary="true"] {
                background: #00897b;
                border-color: #00897b;
                color: #ffffff;
            }
            QPushButton[primary="true"]:hover {
                background: #00796b;
            }
            """
        )

    def refresh_key_status(self) -> None:
        ready = private_key_file().is_file()
        self.key_status.setText("签发密钥已就绪" if ready else "未找到签发密钥")
        if not ready:
            self.key_status.setStyleSheet(
                "background:#fee2e2;color:#b91c1c;border:1px solid #fca5a5;"
                "border-radius:6px;padding:7px 12px;font-weight:700;"
            )

    def paste_machine_code(self) -> None:
        self.machine_input.setText(QApplication.clipboard().text().strip().upper())
        self.machine_input.setFocus()

    def clear_form(self) -> None:
        self.machine_input.clear()
        self.order_input.clear()
        self.permanent_check.setChecked(False)
        self.expiry_input.setDate(QDate.currentDate().addMonths(1))
        self.code_output.clear()
        self.operation_status.clear()
        self.machine_input.setFocus()

    def generate_code(self) -> None:
        machine = self.machine_input.text().strip().upper()
        order_number = self.order_input.text().strip()
        expires_at = "" if self.permanent_check.isChecked() else self.expiry_input.date().toString("yyyy-MM-dd")
        try:
            code = issue_activation_code(
                machine,
                order_number=order_number,
                expires_at=expires_at,
            )
            append_history(
                {
                    "orderNumber": order_number,
                    "machineCode": machine,
                    "expiresAt": expires_at,
                    "code": code,
                }
            )
        except Exception as exc:
            QMessageBox.warning(self, "生成失败", str(exc))
            return
        self.code_output.setPlainText(code)
        QApplication.clipboard().setText(code)
        self.operation_status.setText("已生成并复制")
        self.refresh_history()

    def copy_current_code(self) -> None:
        code = self.code_output.toPlainText().strip()
        if not code:
            QMessageBox.information(self, "复制激活码", "请先生成激活码。")
            return
        QApplication.clipboard().setText(code)
        self.operation_status.setText("本次激活码已复制")

    def refresh_history(self) -> None:
        self.records = load_history()
        visible_records = self.records[:50]
        lines = []
        for index, record in enumerate(visible_records, start=1):
            machine = str(record.get("machineCode") or "")
            masked_machine = f"{machine[:6]}...{machine[-6:]}" if len(machine) > 12 else machine
            lines.append(
                f"{index}. {record.get('createdAt') or ''}  |  "
                f"{record.get('orderNumber') or '未填写'}  |  {masked_machine}  |  "
                f"{record.get('expiresAt') or '长期有效'}"
            )
        self.history_output.setPlainText("\n".join(lines))

    def copy_selected_history(self) -> None:
        raw_index = self.history_index_input.text().strip()
        if not raw_index.isdigit() or int(raw_index) < 1:
            QMessageBox.information(self, "复制记录", "请输入有效的记录序号。")
            return
        row = int(raw_index) - 1
        if row >= min(len(self.records), 50):
            QMessageBox.information(self, "复制记录", "该记录序号不存在。")
            return
        code = str(self.records[row].get("code") or "")
        if not code:
            QMessageBox.warning(self, "复制记录", "该记录没有可复制的激活码。")
            return
        QApplication.clipboard().setText(code)
        self.operation_status.setText("历史激活码已复制")


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setApplicationDisplayName(APP_TITLE)
    icon_path = resource_path("ui/assets/fashetai-app-icon.png")
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))
    window = LicenseIssuerWindow()
    window.show()
    return app.exec()


def run_ui_self_test() -> str:
    """构建验收入口：只验证界面可创建，不读取或生成真实激活码。"""

    app = QApplication.instance() or QApplication([])
    window = LicenseIssuerWindow(load_saved_history=False)
    try:
        if window.windowTitle() != APP_TITLE:
            raise RuntimeError("卖家激活码管理器标题异常")
        if not window.history_output.isReadOnly():
            raise RuntimeError("卖家激活码管理器记录区异常")
        return "SELLER_LICENSE_UI_OK"
    finally:
        window.close()
        app.processEvents()


if __name__ == "__main__":
    if "--ui-test" in sys.argv:
        print(run_ui_self_test())
        raise SystemExit(0)
    raise SystemExit(main())
