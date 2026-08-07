# -*- coding: utf-8 -*-
"""把桌面端运行输出安全地呈现在客户端内。"""

from __future__ import annotations

from collections import deque
from datetime import datetime
import logging
import sys

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)


class RuntimeLogBus(QObject):
    """跨线程汇总标准输出和 logging 记录，最多保留最近 300 条。"""

    line_added = pyqtSignal(str)
    cleared = pyqtSignal()

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._lines: deque[str] = deque(maxlen=300)

    def publish(self, message: object) -> None:
        text = str(message or "").strip()
        if not text:
            return
        line = f"{datetime.now().strftime('%H:%M:%S')}  {text}"
        self._lines.append(line)
        self.line_added.emit(line)

    def history(self) -> list[str]:
        return list(self._lines)

    def clear(self) -> None:
        """清空客户端当前运行日志；后续新日志仍会正常追加。"""

        self._lines.clear()
        self.cleared.emit()


class _RuntimeLogStream:
    """将 print 的分段写入合并为完整日志行。"""

    def __init__(self, bus: RuntimeLogBus) -> None:
        self._bus = bus
        self._buffer = ""

    def write(self, value: object) -> int:
        text = str(value or "")
        self._buffer += text.replace("\r", "")
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._bus.publish(line)
        return len(text)

    def flush(self) -> None:
        if self._buffer.strip():
            self._bus.publish(self._buffer)
        self._buffer = ""

    def isatty(self) -> bool:
        return False


class _RuntimeLogHandler(logging.Handler):
    """将标准 logging 直接投递到客户端，避免依赖控制台。"""

    def __init__(self, bus: RuntimeLogBus) -> None:
        super().__init__()
        self._bus = bus
        self.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._bus.publish(self.format(record))
        except Exception:
            self.handleError(record)


def runtime_log_bus() -> RuntimeLogBus:
    """返回当前 QApplication 绑定的日志总线。"""

    app = QApplication.instance()
    if app is None:
        raise RuntimeError("QApplication 尚未初始化，无法创建执行日志面板")
    bus = getattr(app, "_oneclick_runtime_log_bus", None)
    if not isinstance(bus, RuntimeLogBus):
        bus = RuntimeLogBus(app)
        app._oneclick_runtime_log_bus = bus
    return bus


def install_runtime_log_capture() -> None:
    """在正常桌面启动时接管 stdout、stderr 与根日志器。"""

    app = QApplication.instance()
    if app is None or getattr(app, "_oneclick_runtime_log_capture_installed", False):
        return
    bus = runtime_log_bus()
    app._oneclick_runtime_log_capture_installed = True
    app._oneclick_original_stdout = sys.stdout
    app._oneclick_original_stderr = sys.stderr
    sys.stdout = _RuntimeLogStream(bus)  # type: ignore[assignment]
    sys.stderr = _RuntimeLogStream(bus)  # type: ignore[assignment]
    try:
        # utils.log 在模块导入时保存了旧 stdout；这里显式换到新输出流，才能
        # 收到抖音执行器产生的 INFO/SUCCESS 等真实运行日志。
        from utils.log import redirect_console_logger

        redirect_console_logger(sys.stdout)
    except Exception as exc:
        bus.publish(f"执行日志接管未完整启用：{exc}")
    root_logger = logging.getLogger()
    handler = _RuntimeLogHandler(bus)
    root_logger.addHandler(handler)
    app._oneclick_runtime_log_handler = handler
    bus.publish("客户端运行日志已启用")


class ExecutionLogPanel(QFrame):
    """可复用的只读执行日志面板；多个步骤共享同一份实时记录。"""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("douyinCommerceExecutionLog")
        self.setMinimumWidth(272)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 16)
        layout.setSpacing(10)
        header = QHBoxLayout()
        title = QLabel("执行日志")
        title.setObjectName("douyinCommerceExecutionLogTitle")
        header.addWidget(title)
        header.addStretch(1)
        self.copy_button = QPushButton("复制日志")
        self.copy_button.setObjectName("douyinCommerceCopyExecutionLog")
        self.copy_button.setAccessibleName("复制全部执行日志")
        self.copy_button.clicked.connect(self.copy_execution_log)
        header.addWidget(self.copy_button)
        self.clear_button = QPushButton("清空日志")
        self.clear_button.setObjectName("douyinCommerceClearExecutionLog")
        self.clear_button.setAccessibleName("清空全部执行日志")
        self.clear_button.clicked.connect(self.clear_execution_log)
        header.addWidget(self.clear_button)
        hint = QLabel("实时")
        hint.setObjectName("douyinCommerceExecutionLogHint")
        header.addWidget(hint)
        layout.addLayout(header)
        self.output = QPlainTextEdit()
        self.output.setObjectName("douyinCommerceExecutionLogOutput")
        self.output.setReadOnly(True)
        self.output.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.output.document().setMaximumBlockCount(300)
        self.output.setMinimumHeight(390)
        layout.addWidget(self.output, 1)
        bus = runtime_log_bus()
        history = bus.history()
        if history:
            self.output.setPlainText("\n".join(history))
            self.output.moveCursor(QTextCursor.MoveOperation.End)
        bus.line_added.connect(self._append_line)
        bus.cleared.connect(self.output.clear)

    def _append_line(self, line: str) -> None:
        self.output.appendPlainText(line)
        self.output.moveCursor(QTextCursor.MoveOperation.End)

    def copy_execution_log(self) -> None:
        """复制当前面板内的完整日志，方便排查或反馈问题。"""

        content = self.output.toPlainText().strip()
        if not content:
            self.copy_button.setText("暂无日志")
            return
        QApplication.clipboard().setText(content)
        self.copy_button.setText(f"已复制 {len(content.splitlines())} 条")

    def clear_execution_log(self) -> None:
        """清空所有步骤共用的客户端日志记录。"""

        runtime_log_bus().clear()
        self.clear_button.setText("已清空")
