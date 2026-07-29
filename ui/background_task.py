# -*- coding: utf-8 -*-
"""桌面端后台任务执行器。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal, pyqtSlot


class TaskSignals(QObject):
    """后台任务的跨线程信号。"""

    started = pyqtSignal()
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)
    finished = pyqtSignal()


class BackgroundTask(QRunnable):
    """在线程池里执行一个同步函数，并把结果送回 UI。"""

    def __init__(self, fn: Callable[[], Any]) -> None:
        super().__init__()
        self.fn = fn
        self.signals = TaskSignals()
        self.setAutoDelete(False)

    @pyqtSlot()
    def run(self) -> None:
        self.signals.started.emit()
        try:
            self.signals.succeeded.emit(self.fn())
        except Exception as exc:
            self.signals.failed.emit(str(exc) or exc.__class__.__name__)
        finally:
            self.signals.finished.emit()


class BackgroundTaskRunner(QObject):
    """按任务键去重的后台任务管理器。"""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.pool = QThreadPool.globalInstance()
        self.active: dict[str, BackgroundTask] = {}

    def is_running(self, key: str) -> bool:
        return key in self.active

    def run(
        self,
        key: str,
        fn: Callable[[], Any],
        *,
        on_started: Callable[[], None] | None = None,
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_finished: Callable[[], None] | None = None,
    ) -> bool:
        if key in self.active:
            return False

        task = BackgroundTask(fn)
        self.active[key] = task

        if on_started:
            task.signals.started.connect(on_started)
        if on_success:
            task.signals.succeeded.connect(on_success)
        if on_error:
            task.signals.failed.connect(on_error)

        def cleanup() -> None:
            self.active.pop(key, None)
            if on_finished:
                on_finished()

        task.signals.finished.connect(cleanup)
        self.pool.start(task)
        return True
