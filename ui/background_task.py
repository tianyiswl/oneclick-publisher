# -*- coding: utf-8 -*-
"""桌面端后台任务执行器。"""

from __future__ import annotations

from collections.abc import Callable
import threading
import traceback
from typing import Any

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal, pyqtSlot


class TaskSignals(QObject):
    """后台任务的跨线程信号。"""

    started = pyqtSignal()
    progressed = pyqtSignal(object)
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)
    finished = pyqtSignal()


class BackgroundTask(QRunnable):
    """在线程池里执行一个同步函数，并把结果送回 UI。"""

    def __init__(self, fn: Callable[[Callable[[object], None]], Any]) -> None:
        super().__init__()
        self.fn = fn
        self.signals = TaskSignals()
        self.setAutoDelete(False)
        self._state_lock = threading.Lock()
        self._state = "queued"
        self._finished = threading.Event()

    def cancel_pending(self) -> bool:
        """只取消尚未进入 worker 的任务；与 ``run`` 的抢占由同一把锁裁决。"""

        with self._state_lock:
            if self._state != "queued":
                return False
            self._state = "cancelled"
            self._finished.set()
            return True

    def wait_for_finished(self, timeout_seconds: float) -> bool:
        """等待 worker 结束；事件在线程信号送回 UI 前置位，避免退出死锁。"""

        return self._finished.wait(max(0.0, float(timeout_seconds)))

    def _claim_run(self) -> bool:
        with self._state_lock:
            if self._state == "cancelled":
                return False
            if self._state != "queued":
                return False
            self._state = "running"
            return True

    def _mark_finished(self) -> None:
        with self._state_lock:
            self._state = "finished"
            self._finished.set()

    @pyqtSlot()
    def run(self) -> None:
        if not self._claim_run():
            self.signals.finished.emit()
            return
        self.signals.started.emit()
        try:
            self.signals.succeeded.emit(self.fn(self.signals.progressed.emit))
        except Exception as exc:
            self.signals.failed.emit(str(exc) or exc.__class__.__name__)
        finally:
            self._mark_finished()
            self.signals.finished.emit()


class BackgroundTaskRunner(QObject):
    """按任务键去重的后台任务管理器。"""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.pool = QThreadPool.globalInstance()
        self.active: dict[str, BackgroundTask] = {}
        self._active_lock = threading.RLock()

    def is_running(self, key: str) -> bool:
        with self._active_lock:
            return key in self.active

    def cancel_pending(self, key: str) -> bool:
        """原子取消尚未开跑的指定任务，并立即撤销其 active 投影。"""

        with self._active_lock:
            task = self.active.get(key)
            if task is None or not task.cancel_pending():
                return False
            self.active.pop(key, None)
            return True

    def wait_for_finished(self, key: str, timeout_seconds: float) -> bool:
        """有界等待指定 worker；active 清理由 Qt finished 回调稍后完成。"""

        with self._active_lock:
            task = self.active.get(key)
        if task is None:
            return True
        return task.wait_for_finished(timeout_seconds)

    def active_keys_with_prefixes(self, prefixes: tuple[str, ...]) -> list[str]:
        """Return a stable snapshot of owned dynamic task keys."""

        if not isinstance(prefixes, tuple) or not all(
            isinstance(prefix, str) and prefix for prefix in prefixes
        ):
            raise ValueError("后台任务前缀无效")
        with self._active_lock:
            return sorted(
                key
                for key in self.active
                if any(
                    key == prefix or key.startswith(f"{prefix}:")
                    for prefix in prefixes
                )
            )

    def run(
        self,
        key: str,
        fn: Callable[[], Any] | None = None,
        *,
        with_progress: Callable[[Callable[[object], None]], Any] | None = None,
        on_started: Callable[[], None] | None = None,
        on_progress: Callable[[object], None] | None = None,
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_finished: Callable[[], None] | None = None,
    ) -> bool:
        if fn is None and with_progress is None:
            raise ValueError("后台任务必须提供执行函数")

        if with_progress is not None:
            worker = with_progress
        else:
            assert fn is not None

            def worker(_report: Callable[[object], None]) -> Any:
                return fn()

        task = BackgroundTask(worker)
        with self._active_lock:
            if key in self.active:
                return False
            self.active[key] = task

        if on_started:
            task.signals.started.connect(on_started)
        if on_progress:
            task.signals.progressed.connect(on_progress)
        if on_success:
            def deliver_success(value: object) -> None:
                try:
                    on_success(value)
                except Exception as exc:
                    # PyQt6 信号槽中的未捕获异常可能直接终止整个进程。后台任务
                    # 的结果处理也必须进入错误通道，不能让单个功能拖垮客户端。
                    message = f"处理后台任务结果失败：{str(exc) or exc.__class__.__name__}"
                    if on_error:
                        try:
                            on_error(message)
                        except Exception:
                            traceback.print_exc()
                    else:
                        traceback.print_exc()

            task.signals.succeeded.connect(deliver_success)
        if on_error:
            task.signals.failed.connect(on_error)

        def cleanup() -> None:
            with self._active_lock:
                if self.active.get(key) is not task:
                    return
                self.active.pop(key, None)
            if on_finished:
                on_finished()

        task.signals.finished.connect(cleanup)
        self.pool.start(task)
        return True
