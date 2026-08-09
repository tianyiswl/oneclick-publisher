# -*- coding: utf-8 -*-
"""抖音带货平台设置的三会话隔离采集协调器。

本模块只协调内置探针上传、收藏音乐和地点候选读取；不保存或提交
草稿，也不对外暴露 Cookie、验证码、二维码、页面 HTML 或底层异常原文。
"""

from __future__ import annotations

from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass, field
import threading
from typing import Any, Callable, Mapping
from uuid import uuid4

from .douyin_commerce_probe import build_probe_upload_payload
from .douyin_commerce_session import DouyinCommerceSessionManager
from .douyin_commerce_setup_state import (
    CollectorDiagnosticEvent,
    CollectorState,
    CollectorType,
    SetupGeneration,
    SetupGenerationState,
    new_setup_generation,
)


class DouyinCommerceCollectorError(RuntimeError):
    """采集协调器的固定、可公开错误码。"""

    def __init__(self, code: str, *, event_emitted: bool = False) -> None:
        self.code = str(code or "collector_unknown")
        self.event_emitted = event_emitted
        super().__init__(self.code)


@dataclass
class _CollectorRuntime:
    collector_type: CollectorType
    manager: Any
    instance_id: str
    session_id: str = ""
    fixed_scope: str = ""


@dataclass
class _GenerationRuntime:
    generation: SetupGeneration
    probe_payload: dict[str, Any]
    collectors: dict[CollectorType, _CollectorRuntime]
    cleanup_results: dict[CollectorType, str] = field(default_factory=dict)
    cleanup_actions: dict[CollectorType, _QueuedAction] = field(default_factory=dict)
    closed_result: dict[str, object] | None = None


@dataclass
class _QueuedAction:
    generation_id: str
    request_id: str
    collector_type: CollectorType
    callback: Callable[[], Any]
    is_cleanup: bool = False
    ready: threading.Event = field(default_factory=threading.Event)
    future: Future[Any] | None = None


class _CollectorActionQueue:
    """所有平台动作共用的单工作线程队列。"""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="douyin-commerce-collector-action",
        )
        self._lock = threading.RLock()
        self._actions: list[_QueuedAction] = []
        self._running: _QueuedAction | None = None

    def submit(
        self,
        generation_id: str,
        request_id: str,
        collector_type: CollectorType,
        callback: Callable[[], Any],
        *,
        is_cleanup: bool = False,
    ) -> _QueuedAction:
        action = _QueuedAction(
            generation_id=generation_id,
            request_id=request_id,
            collector_type=collector_type,
            callback=callback,
            is_cleanup=is_cleanup,
        )
        with self._lock:
            self._actions.append(action)
            action.future = self._executor.submit(self._execute, action)
            action.ready.set()
        return action

    def wait(self, action: _QueuedAction) -> Any:
        action.ready.wait()
        future = action.future
        if future is None:
            raise DouyinCommerceCollectorError("collector_unknown")
        try:
            return future.result()
        except CancelledError:
            raise DouyinCommerceCollectorError("stale_result_discarded") from None

    def run(
        self,
        generation_id: str,
        request_id: str,
        collector_type: CollectorType,
        callback: Callable[[], Any],
    ) -> Any:
        """入队并同步等待本次动作。"""

        return self.wait(
            self.submit(generation_id, request_id, collector_type, callback)
        )

    def cancel_pending(self, generation_id: str) -> None:
        with self._lock:
            actions = list(self._actions)
            running = self._running
        for action in actions:
            future = action.future
            if (
                action.generation_id == generation_id
                and action is not running
                and not action.is_cleanup
                and future is not None
                and not future.done()
            ):
                if future.cancel():
                    with self._lock:
                        if action in self._actions and action is not self._running:
                            self._actions.remove(action)

    def wait_running(self, generation_id: str, timeout: float = 15) -> bool:
        with self._lock:
            action = self._running
            if action is None or action.generation_id != generation_id:
                return True
            future = action.future
        if future is None:
            return True
        try:
            future.result(timeout=timeout)
        except TimeoutError:
            return False
        except Exception:
            # 此处只关心动作是否已结束，业务错误由原调用方接收。
            return True
        return True

    def wait_action(self, action: _QueuedAction, timeout: float = 15) -> bool:
        action.ready.wait()
        future = action.future
        if future is None:
            return True
        try:
            future.result(timeout=timeout)
        except TimeoutError:
            return False
        except Exception:
            return True
        return True

    def running_collector_type(self, generation_id: str) -> CollectorType | None:
        with self._lock:
            action = self._running
            if action is None or action.generation_id != generation_id:
                return None
            return action.collector_type

    def _execute(self, action: _QueuedAction) -> Any:
        action.ready.wait()
        with self._lock:
            self._running = action
        try:
            return action.callback()
        finally:
            with self._lock:
                if self._running is action:
                    self._running = None
                if action in self._actions:
                    self._actions.remove(action)


_COLLECTOR_ORDER = (
    CollectorType.DOMESTIC_LOCATION,
    CollectorType.FAVORITE_MUSIC,
    CollectorType.LOCAL_LOCATION,
)

_FIXED_SCOPES = {
    CollectorType.DOMESTIC_LOCATION: "domestic",
    CollectorType.FAVORITE_MUSIC: "",
    CollectorType.LOCAL_LOCATION: "local",
}

_LOCATION_COLLECTORS = {
    "domestic": CollectorType.DOMESTIC_LOCATION,
    "local": CollectorType.LOCAL_LOCATION,
}


class DouyinCommerceCollectorManager:
    """以代际隔离三个独立抖音平台设置采集会话。"""

    def __init__(
        self,
        manager_factory: Callable[[], Any] = DouyinCommerceSessionManager,
        probe_payload_builder: Callable[[Mapping[str, Any]], Mapping[str, Any]] = (
            build_probe_upload_payload
        ),
        event_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self._manager_factory = manager_factory
        self._probe_payload_builder = probe_payload_builder
        self._event_sink = event_sink
        self._lifecycle_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._action_queue = _CollectorActionQueue()
        self._runtime: _GenerationRuntime | None = None

    def begin_generation(
        self,
        payload: Mapping[str, Any],
        *,
        on_progress=None,
    ) -> dict[str, object]:
        """关闭旧代际，建立新代际并只自动启动国内地点采集器。"""

        with self._lifecycle_lock:
            return self._begin_generation(payload, on_progress=on_progress)

    def _begin_generation(
        self,
        payload: Mapping[str, Any],
        *,
        on_progress=None,
    ) -> dict[str, object]:

        if not isinstance(payload, Mapping):
            raise DouyinCommerceCollectorError("collector_start_failed")

        with self._state_lock:
            previous = self._runtime
            previous_id = previous.generation.generation_id if previous else ""
            needs_close = bool(
                previous
                and (
                    previous.generation.state is not SetupGenerationState.CLOSED
                    or previous.collectors
                )
            )
        if needs_close:
            cleanup = self.close_generation(previous_id, reason="replaced")
            if cleanup.get("closed") is not True or int(
                cleanup.get("aliveCollectorCount") or 0
            ):
                raise DouyinCommerceCollectorError("cleanup_incomplete")

        try:
            probe_payload = dict(self._probe_payload_builder(payload))
        except Exception:
            raise DouyinCommerceCollectorError("collector_start_failed") from None

        generation = new_setup_generation(account_id=self._account_id(payload))
        generation.transition(SetupGenerationState.COLLECTING)
        runtime = _GenerationRuntime(
            generation=generation,
            probe_payload=probe_payload,
            collectors={},
        )
        with self._state_lock:
            self._runtime = runtime
            action = self._enqueue_locked(
                runtime,
                CollectorType.DOMESTIC_LOCATION,
                lambda: self._ensure_collector(
                    generation.generation_id,
                    CollectorType.DOMESTIC_LOCATION,
                    on_progress=on_progress,
                ),
            )
        try:
            self._action_queue.wait(action)
        except DouyinCommerceCollectorError:
            raise
        except Exception:
            raise DouyinCommerceCollectorError("collector_start_failed") from None
        return self.status(generation.generation_id)

    def refresh_favorite_music(
        self, generation_id: str
    ) -> list[dict[str, str]]:
        """在独立音乐会话中懒启动并刷新收藏音乐。"""

        request_id = str(uuid4())
        with self._state_lock:
            runtime = self._require_collecting_runtime(generation_id)
            action = self._action_queue.submit(
                generation_id,
                request_id,
                CollectorType.FAVORITE_MUSIC,
                lambda: self._refresh_music_action(generation_id, request_id),
            )
        return self._wait_public_action(
            action,
            generation_id=generation_id,
            collector_type=CollectorType.FAVORITE_MUSIC,
            request_id=request_id,
            action_name="refresh_favorite_music",
        )

    def search_locations(
        self,
        generation_id: str,
        keyword: object,
        scope: object,
    ) -> list[dict[str, Any]]:
        """按固定范围将地点搜索路由到独立会话。"""

        normalized_scope = str(scope or "").strip().casefold()
        collector_type = _LOCATION_COLLECTORS.get(normalized_scope)
        if collector_type is None:
            raise DouyinCommerceCollectorError("collector_scope_mismatch")
        request_id = str(uuid4())
        with self._state_lock:
            self._require_collecting_runtime(generation_id)
            action = self._action_queue.submit(
                generation_id,
                request_id,
                collector_type,
                lambda: self._search_locations_action(
                    generation_id,
                    collector_type,
                    request_id,
                    keyword,
                    normalized_scope,
                ),
            )
        return self._wait_public_action(
            action,
            generation_id=generation_id,
            collector_type=collector_type,
            request_id=request_id,
            action_name="search_locations",
            scope=normalized_scope,
            keyword=keyword,
        )

    def retry_collector(
        self,
        generation_id: str,
        collector_type: object,
    ) -> dict[str, object]:
        """在一个队列动作中只替换指定采集器。"""

        normalized_type = self._collector_type(collector_type)
        request_id = str(uuid4())
        with self._state_lock:
            runtime = self._require_collecting_runtime(generation_id)
            current = runtime.collectors.get(normalized_type)
            expected_instance_id = current.instance_id if current else ""
            action = self._action_queue.submit(
                generation_id,
                request_id,
                normalized_type,
                lambda: self._retry_collector_action(
                    generation_id,
                    normalized_type,
                    expected_instance_id,
                ),
            )
        self._wait_public_action(
            action,
            generation_id=generation_id,
            collector_type=normalized_type,
            request_id=request_id,
            action_name="retry_collector",
            scope=_FIXED_SCOPES[normalized_type],
        )
        return self.status(generation_id)

    def close_generation(
        self,
        generation_id: str | None = None,
        *,
        reason: str,
    ) -> dict[str, object]:
        """幂等取消队列并尽力关闭当前代际的所有会话。"""

        with self._lifecycle_lock:
            return self._close_generation(generation_id, reason=reason)

    def _close_generation(
        self,
        generation_id: str | None = None,
        *,
        reason: str,
    ) -> dict[str, object]:

        normalized_reason = str(reason or "").strip().casefold()
        is_publish_barrier = any(
            marker in normalized_reason
            for marker in ("publish", "preflight", "continue", "check")
        )
        with self._state_lock:
            runtime = self._runtime
            if runtime is None:
                return self._empty_close_result(generation_id)
            current_id = runtime.generation.generation_id
            if generation_id and generation_id != current_id:
                raise DouyinCommerceCollectorError("stale_result_discarded")
            if runtime.closed_result is not None and not runtime.collectors:
                return self._copy_close_result(runtime.closed_result)
            generation = runtime.generation
            if generation.state in {
                SetupGenerationState.NEW,
                SetupGenerationState.COLLECTING,
                SetupGenerationState.READY,
            }:
                generation.transition(
                    SetupGenerationState.CLOSING_COLLECTORS
                    if is_publish_barrier
                    else SetupGenerationState.CANCELLING
                )
            for collector_type, collector in runtime.collectors.items():
                slot = generation.collectors[collector_type]
                if slot.instance_id == collector.instance_id:
                    slot.state = CollectorState.CLOSING

        self._action_queue.cancel_pending(current_id)
        running_type = self._action_queue.running_collector_type(current_id)
        running_finished = self._action_queue.wait_running(current_id, timeout=15)

        for collector_type in _COLLECTOR_ORDER:
            with self._state_lock:
                collector = runtime.collectors.get(collector_type)
                previous_result = runtime.cleanup_results.get(collector_type)
                existing_cleanup = runtime.cleanup_actions.get(collector_type)
            if existing_cleanup is not None:
                self._action_queue.wait_action(existing_cleanup, timeout=15)
                continue
            if collector is None:
                if previous_result is None:
                    runtime.cleanup_results[collector_type] = "not_started"
                continue

            if not running_finished and collector_type is running_type:
                with self._state_lock:
                    runtime.cleanup_results[collector_type] = "cleanup_incomplete"
                    self._defer_collector_close_locked(
                        runtime,
                        collector_type,
                        collector,
                    )
                continue

            cleanup_result = "closed"
            try:
                collector.manager.close(collector.session_id or None)
            except Exception:
                cleanup_result = "cleanup_incomplete"
            with self._state_lock:
                slot = generation.collectors[collector_type]
                slot.state = CollectorState.CLOSED
                slot.session_id = None
                runtime.cleanup_results[collector_type] = cleanup_result
                if cleanup_result == "closed":
                    if runtime.collectors.get(collector_type) is collector:
                        runtime.collectors.pop(collector_type, None)

        with self._state_lock:
            for collector_type in _COLLECTOR_ORDER:
                runtime.cleanup_results.setdefault(collector_type, "not_started")
            alive_count = len(runtime.collectors)
            closed = running_finished and alive_count == 0 and all(
                result != "cleanup_incomplete"
                for result in runtime.cleanup_results.values()
            )
            if closed:
                generation.close()
            result = {
                "closed": closed,
                "setupGenerationId": current_id,
                "aliveCollectorCount": alive_count,
                "cleanupResults": {
                    collector_type.value: runtime.cleanup_results[collector_type]
                    for collector_type in _COLLECTOR_ORDER
                },
            }
            if closed:
                runtime.closed_result = self._copy_close_result(result)
            return result

    def _defer_collector_close_locked(
        self,
        runtime: _GenerationRuntime,
        collector_type: CollectorType,
        collector: _CollectorRuntime,
    ) -> None:
        existing = runtime.cleanup_actions.get(collector_type)
        if existing is not None and existing.future is not None and not existing.future.done():
            return
        action = self._action_queue.submit(
            runtime.generation.generation_id,
            str(uuid4()),
            collector_type,
            lambda: self._finish_deferred_collector_close(
                runtime,
                collector_type,
                collector,
            ),
            is_cleanup=True,
        )
        runtime.cleanup_actions[collector_type] = action

    def _finish_deferred_collector_close(
        self,
        runtime: _GenerationRuntime,
        collector_type: CollectorType,
        collector: _CollectorRuntime,
    ) -> None:
        cleanup_result = "closed"
        try:
            collector.manager.close(collector.session_id or None)
        except Exception:
            cleanup_result = "cleanup_incomplete"

        with self._state_lock:
            runtime.cleanup_actions.pop(collector_type, None)
            runtime.cleanup_results[collector_type] = cleanup_result
            slot = runtime.generation.collectors[collector_type]
            if slot.instance_id == collector.instance_id:
                slot.state = CollectorState.CLOSED
                slot.session_id = None
            if (
                cleanup_result == "closed"
                and runtime.collectors.get(collector_type) is collector
            ):
                runtime.collectors.pop(collector_type, None)
            self._finalize_closed_runtime_locked(runtime)

    def _finalize_closed_runtime_locked(self, runtime: _GenerationRuntime) -> None:
        if runtime.collectors or runtime.cleanup_actions:
            return
        if any(
            result == "cleanup_incomplete"
            for result in runtime.cleanup_results.values()
        ):
            return
        for collector_type in _COLLECTOR_ORDER:
            runtime.cleanup_results.setdefault(collector_type, "not_started")
        runtime.generation.close()
        runtime.closed_result = {
            "closed": True,
            "setupGenerationId": runtime.generation.generation_id,
            "aliveCollectorCount": 0,
            "cleanupResults": {
                collector_type.value: runtime.cleanup_results[collector_type]
                for collector_type in _COLLECTOR_ORDER
            },
        }

    def status(self, generation_id: str | None = None) -> dict[str, object]:
        """返回不包含底层 Session ID 的公开状态。"""

        with self._state_lock:
            runtime = self._runtime
            if runtime is None:
                return {
                    "setupGenerationId": generation_id or "",
                    "generationState": SetupGenerationState.CLOSED.value,
                    "collectors": {
                        collector_type.value: CollectorState.NOT_STARTED.value
                        for collector_type in _COLLECTOR_ORDER
                    },
                    "collectorInstanceIds": {
                        collector_type.value: "" for collector_type in _COLLECTOR_ORDER
                    },
                    "aliveCollectorCount": 0,
                }
            if generation_id and generation_id != runtime.generation.generation_id:
                raise DouyinCommerceCollectorError("stale_result_discarded")
            generation = runtime.generation
            return {
                "setupGenerationId": generation.generation_id,
                "generationState": generation.state.value,
                "collectors": {
                    collector_type.value: generation.collectors[
                        collector_type
                    ].state.value
                    for collector_type in _COLLECTOR_ORDER
                },
                "collectorInstanceIds": {
                    collector_type.value: (
                        generation.collectors[collector_type].instance_id or ""
                    )
                    for collector_type in _COLLECTOR_ORDER
                },
                "aliveCollectorCount": len(runtime.collectors),
            }

    def _enqueue_locked(
        self,
        runtime: _GenerationRuntime,
        collector_type: CollectorType,
        callback: Callable[[], Any],
    ) -> _QueuedAction:
        self._require_collecting_runtime(runtime.generation.generation_id)
        return self._action_queue.submit(
            runtime.generation.generation_id,
            str(uuid4()),
            collector_type,
            callback,
        )

    def _refresh_music_action(
        self,
        generation_id: str,
        request_id: str,
    ) -> list[dict[str, str]]:
        collector = self._ensure_collector(
            generation_id, CollectorType.FAVORITE_MUSIC
        )
        self._validate_active_collector(generation_id, collector)
        try:
            result = collector.manager.refresh_favorite_music(collector.session_id)
        except Exception:
            if not self._mark_failed(generation_id, collector):
                self._emit_event(
                    request_id=request_id,
                    generation_id=generation_id,
                    collector=collector,
                    phase="result",
                    action="refresh_favorite_music",
                    scope="",
                    keyword="",
                    outcome="discarded",
                    error_code="stale_result_discarded",
                )
                raise DouyinCommerceCollectorError(
                    "stale_result_discarded", event_emitted=True
                ) from None
            raise DouyinCommerceCollectorError("collector_unknown") from None
        try:
            public_result = [dict(item) for item in result]
        except Exception:
            if not self._mark_failed(generation_id, collector):
                raise DouyinCommerceCollectorError(
                    "stale_result_discarded"
                ) from None
            raise DouyinCommerceCollectorError("collector_unknown") from None
        self._accept_or_discard(
            generation_id,
            collector,
            request_id=request_id,
            action="refresh_favorite_music",
        )
        return public_result

    def _search_locations_action(
        self,
        generation_id: str,
        collector_type: CollectorType,
        request_id: str,
        keyword: object,
        scope: str,
    ) -> list[dict[str, Any]]:
        collector = self._ensure_collector(generation_id, collector_type)
        if collector.fixed_scope != scope:
            raise DouyinCommerceCollectorError("collector_scope_mismatch")
        self._validate_active_collector(generation_id, collector)
        try:
            result = collector.manager.search_locations(
                collector.session_id,
                keyword,
                scope,
            )
        except Exception:
            if not self._mark_failed(generation_id, collector):
                self._emit_event(
                    request_id=request_id,
                    generation_id=generation_id,
                    collector=collector,
                    phase="result",
                    action="search_locations",
                    scope=scope,
                    keyword=keyword,
                    outcome="discarded",
                    error_code="stale_result_discarded",
                )
                raise DouyinCommerceCollectorError(
                    "stale_result_discarded", event_emitted=True
                ) from None
            raise DouyinCommerceCollectorError("collector_unknown") from None
        try:
            public_result = [dict(item) for item in result]
        except Exception:
            if not self._mark_failed(generation_id, collector):
                raise DouyinCommerceCollectorError(
                    "stale_result_discarded"
                ) from None
            raise DouyinCommerceCollectorError("collector_unknown") from None
        self._accept_or_discard(
            generation_id,
            collector,
            request_id=request_id,
            action="search_locations",
            scope=scope,
            keyword=keyword,
        )
        return public_result

    def _retry_collector_action(
        self,
        generation_id: str,
        collector_type: CollectorType,
        expected_instance_id: str,
    ) -> _CollectorRuntime:
        with self._state_lock:
            runtime = self._require_collecting_runtime(generation_id)
            old = runtime.collectors.get(collector_type)
            current_instance_id = old.instance_id if old else ""
            if current_instance_id != expected_instance_id:
                raise DouyinCommerceCollectorError("stale_result_discarded")
            slot = runtime.generation.collectors[collector_type]
            slot.state = CollectorState.RETRYING

        if old is not None:
            try:
                old.manager.close(old.session_id or None)
            except Exception:
                with self._state_lock:
                    slot.state = CollectorState.FAILED
                    runtime.cleanup_results[collector_type] = "cleanup_incomplete"
                raise DouyinCommerceCollectorError("cleanup_incomplete") from None
            with self._state_lock:
                if runtime.collectors.get(collector_type) is not old:
                    raise DouyinCommerceCollectorError("stale_result_discarded")
                runtime.collectors.pop(collector_type, None)
                runtime.cleanup_results.pop(collector_type, None)
                slot.instance_id = None
                slot.session_id = None
                slot.state = CollectorState.RETRYING

        return self._ensure_collector(generation_id, collector_type)

    def _ensure_collector(
        self,
        generation_id: str,
        collector_type: CollectorType,
        *,
        on_progress=None,
    ) -> _CollectorRuntime:
        with self._state_lock:
            runtime = self._require_collecting_runtime(generation_id)
            existing = runtime.collectors.get(collector_type)
            if existing is not None:
                if existing.fixed_scope != _FIXED_SCOPES[collector_type]:
                    raise DouyinCommerceCollectorError("collector_scope_mismatch")
                self._validate_active_collector(generation_id, existing)
                return existing

            try:
                manager = self._manager_factory()
            except Exception:
                runtime.generation.collectors[
                    collector_type
                ].state = CollectorState.FAILED
                raise DouyinCommerceCollectorError(
                    "collector_start_failed"
                ) from None
            if any(item.manager is manager for item in runtime.collectors.values()):
                raise DouyinCommerceCollectorError("collector_start_failed")
            collector = _CollectorRuntime(
                collector_type=collector_type,
                manager=manager,
                instance_id=str(uuid4()),
                fixed_scope=_FIXED_SCOPES[collector_type],
            )
            runtime.collectors[collector_type] = collector
            slot = runtime.generation.collectors[collector_type]
            slot.state = CollectorState.STARTING
            slot.instance_id = collector.instance_id
            slot.session_id = None
            probe_payload = dict(runtime.probe_payload)

        try:
            started = manager.start_upload(probe_payload, on_progress=on_progress)
            session_id = str((started or {}).get("sessionId") or "").strip()
            if not session_id:
                raise DouyinCommerceCollectorError("collector_start_failed")
        except DouyinCommerceCollectorError:
            self._mark_failed(generation_id, collector)
            raise
        except Exception:
            self._mark_failed(generation_id, collector)
            raise DouyinCommerceCollectorError("collector_start_failed") from None

        with self._state_lock:
            collector.session_id = session_id
            current = self._runtime
            slot = runtime.generation.collectors[collector_type]
            can_activate = bool(
                current is runtime
                and current.generation.generation_id == generation_id
                and current.generation.state
                in {SetupGenerationState.COLLECTING, SetupGenerationState.READY}
                and current.collectors.get(collector_type) is collector
                and slot.instance_id == collector.instance_id
                and slot.state is CollectorState.STARTING
            )
            has_deferred_cleanup = collector_type in runtime.cleanup_actions
        if not can_activate:
            if not has_deferred_cleanup:
                self._discard_unaccepted_start(runtime, collector)
            raise DouyinCommerceCollectorError("stale_result_discarded")

        with self._state_lock:
            if any(
                item is not collector and item.session_id == session_id
                for item in runtime.collectors.values()
            ):
                slot.state = CollectorState.FAILED
                raise DouyinCommerceCollectorError("collector_start_failed")
            runtime.generation.activate_collector(
                collector_type,
                instance_id=collector.instance_id,
                session_id=session_id,
            )
            return collector

    def _discard_unaccepted_start(
        self,
        runtime: _GenerationRuntime,
        collector: _CollectorRuntime,
    ) -> None:
        cleanup_result = "closed"
        try:
            collector.manager.close(collector.session_id or None)
        except Exception:
            cleanup_result = "cleanup_incomplete"
        with self._state_lock:
            runtime.cleanup_results[collector.collector_type] = cleanup_result
            if (
                cleanup_result == "closed"
                and runtime.collectors.get(collector.collector_type) is collector
            ):
                runtime.collectors.pop(collector.collector_type, None)

    def _validate_active_collector(
        self,
        generation_id: str,
        collector: _CollectorRuntime,
    ) -> None:
        with self._state_lock:
            runtime = self._require_collecting_runtime(generation_id)
            current = runtime.collectors.get(collector.collector_type)
            slot = runtime.generation.collectors[collector.collector_type]
            if (
                current is not collector
                or slot.instance_id != collector.instance_id
                or slot.state is not CollectorState.ACTIVE
            ):
                raise DouyinCommerceCollectorError("stale_result_discarded")

    def _accept_or_discard(
        self,
        generation_id: str,
        collector: _CollectorRuntime,
        *,
        request_id: str,
        action: str,
        scope: str = "",
        keyword: object = "",
    ) -> None:
        with self._state_lock:
            runtime = self._runtime
            accepted = bool(
                runtime
                and runtime.generation.generation_id == generation_id
                and runtime.collectors.get(collector.collector_type) is collector
                and runtime.generation.accepts_result(
                    generation_id,
                    collector.collector_type,
                    collector.instance_id,
                )
            )
        if accepted:
            return
        self._emit_event(
            request_id=request_id,
            generation_id=generation_id,
            collector=collector,
            phase="result",
            action=action,
            scope=scope,
            keyword=keyword,
            outcome="discarded",
            error_code="stale_result_discarded",
        )
        raise DouyinCommerceCollectorError(
            "stale_result_discarded", event_emitted=True
        )

    def _wait_public_action(
        self,
        queued_action: _QueuedAction,
        *,
        generation_id: str,
        collector_type: CollectorType,
        request_id: str,
        action_name: str,
        scope: str = "",
        keyword: object = "",
    ) -> Any:
        try:
            return self._action_queue.wait(queued_action)
        except DouyinCommerceCollectorError as error:
            if error.code == "stale_result_discarded":
                with self._state_lock:
                    runtime = self._runtime
                    collector = (
                        runtime.collectors.get(collector_type)
                        if runtime
                        and runtime.generation.generation_id == generation_id
                        else None
                    )
                if collector is not None and not error.event_emitted:
                    self._emit_event(
                        request_id=request_id,
                        generation_id=generation_id,
                        collector=collector,
                        phase="queue",
                        action=action_name,
                        scope=scope,
                        keyword=keyword,
                        outcome="discarded",
                        error_code=error.code,
                    )
            raise

    def _mark_failed(
        self,
        generation_id: str,
        collector: _CollectorRuntime,
    ) -> bool:
        with self._state_lock:
            runtime = self._runtime
            valid = False
            if (
                runtime
                and runtime.generation.generation_id == generation_id
                and runtime.collectors.get(collector.collector_type) is collector
            ):
                slot = runtime.generation.collectors[collector.collector_type]
                valid = bool(
                    runtime.generation.state
                    in {SetupGenerationState.COLLECTING, SetupGenerationState.READY}
                    and slot.instance_id == collector.instance_id
                    and slot.state
                    in {CollectorState.STARTING, CollectorState.ACTIVE}
                )
                if valid:
                    slot.state = CollectorState.FAILED
            return valid

    def _require_collecting_runtime(
        self,
        generation_id: str,
    ) -> _GenerationRuntime:
        runtime = self._runtime
        if (
            runtime is None
            or runtime.generation.generation_id != str(generation_id or "")
            or runtime.generation.state
            not in {SetupGenerationState.COLLECTING, SetupGenerationState.READY}
        ):
            raise DouyinCommerceCollectorError("stale_result_discarded")
        return runtime

    def _emit_event(
        self,
        *,
        request_id: str,
        generation_id: str,
        collector: _CollectorRuntime,
        phase: str,
        action: str,
        scope: str,
        keyword: object,
        outcome: str,
        error_code: str,
    ) -> None:
        sink = self._event_sink
        if sink is None:
            return
        with self._state_lock:
            runtime = self._runtime
            account_id = (
                runtime.generation.account_id
                if runtime and runtime.generation.generation_id == generation_id
                else 0
            )
        event = CollectorDiagnosticEvent(
            request_id=request_id,
            setup_generation_id=generation_id,
            collector_type=collector.collector_type,
            collector_instance_id=collector.instance_id,
            account_masked_id=f"account-{account_id}",
            phase=phase,
            action=action,
            scope=scope,
            keyword=str(keyword or ""),
            attempt=1,
            candidate_count=0,
            duration_ms=0,
            outcome=outcome,
            error_code=error_code,
            cleanup_result="",
        ).to_public_dict()
        try:
            sink(event)
        except Exception:
            # 诊断消费方不得打断受控采集流程。
            return

    @staticmethod
    def _collector_type(value: object) -> CollectorType:
        if isinstance(value, CollectorType):
            return value
        try:
            return CollectorType(str(value or "").strip().casefold())
        except ValueError:
            raise DouyinCommerceCollectorError("collector_unknown") from None

    @staticmethod
    def _account_id(payload: Mapping[str, Any]) -> int:
        value: object = payload.get("accountId", 0)
        if not value:
            accounts = payload.get("accountList")
            if isinstance(accounts, list) and accounts and isinstance(accounts[0], Mapping):
                value = accounts[0].get("id", 0)
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _empty_close_result(generation_id: str | None) -> dict[str, object]:
        return {
            "closed": True,
            "setupGenerationId": generation_id or "",
            "aliveCollectorCount": 0,
            "cleanupResults": {
                collector_type.value: "not_started"
                for collector_type in _COLLECTOR_ORDER
            },
        }

    @staticmethod
    def _copy_close_result(result: Mapping[str, object]) -> dict[str, object]:
        return {
            **result,
            "cleanupResults": dict(result.get("cleanupResults") or {}),
        }


commerce_collector_manager = DouyinCommerceCollectorManager()
