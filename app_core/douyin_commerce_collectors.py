# -*- coding: utf-8 -*-
"""抖音带货平台设置的三会话隔离采集协调器。

本模块只协调内置探针上传、收藏音乐和地点候选读取；不保存或提交
草稿，也不对外暴露 Cookie、验证码、二维码、页面 HTML 或底层异常原文。
"""

from __future__ import annotations

from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from dataclasses import dataclass, field
import threading
from time import monotonic
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

    def __init__(
        self,
        code: str,
        *,
        event_emitted: bool = False,
        generation_id: str = "",
        collector_type: str = "",
        collector_instance_id: str = "",
    ) -> None:
        self.code = str(code or "collector_unknown")
        self.event_emitted = event_emitted
        self.generation_id = str(generation_id or "")
        self.collector_type = str(collector_type or "")
        self.collector_instance_id = str(collector_instance_id or "")
        super().__init__(self.code)

    def to_public_action_result(self) -> dict[str, object]:
        """将动作失败投影为不含底层会话与异常原文的受控结果。"""

        return {
            "ok": False,
            "errorCode": self.code,
            "setupGenerationId": self.generation_id,
            "collectorType": self.collector_type,
            "collectorInstanceId": self.collector_instance_id,
        }


@dataclass
class _CollectorRuntime:
    collector_type: CollectorType
    manager: Any | None
    instance_id: str
    session_id: str = ""
    fixed_scope: str = ""
    close_owner: _CollectorCloseOwner | None = None


@dataclass
class _CollectorCloseOwner:
    done: threading.Event = field(default_factory=threading.Event)
    result: str = "cleanup_incomplete"


@dataclass
class _LifecycleFlight:
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, object] | None = None
    cancelled: bool = False


@dataclass
class _GenerationRuntime:
    generation: SetupGeneration
    probe_payload: dict[str, Any]
    collectors: dict[CollectorType, _CollectorRuntime]
    cleanup_results: dict[CollectorType, str] = field(default_factory=dict)
    closed_result: dict[str, object] | None = None
    close_flight: _LifecycleFlight | None = None


@dataclass
class _QueuedAction:
    generation_id: str
    request_id: str
    collector_type: CollectorType
    callback: Callable[[], Any]
    is_cleanup: bool = False
    cancelled: bool = False
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
        self._closed_generations: set[str] = set()

    def submit(
        self,
        generation_id: str,
        request_id: str,
        collector_type: CollectorType,
        callback: Callable[[], Any],
        *,
        is_cleanup: bool = False,
    ) -> _QueuedAction:
        action = self.reserve(
            generation_id,
            request_id,
            collector_type,
            callback,
            is_cleanup=is_cleanup,
        )
        self.start(action)
        return action

    def reserve(
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
            if generation_id in self._closed_generations and not is_cleanup:
                action.cancelled = True
                action.ready.set()
            else:
                self._actions.append(action)
        return action

    def start(self, action: _QueuedAction) -> None:
        with self._lock:
            if action.cancelled or action not in self._actions:
                action.cancelled = True
                action.ready.set()
                return
            action.future = self._executor.submit(self._execute, action)
            action.ready.set()

    def wait(self, action: _QueuedAction) -> Any:
        action.ready.wait()
        if action.cancelled:
            raise DouyinCommerceCollectorError("stale_result_discarded") from None
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
            for action in list(self._actions):
                future = action.future
                if (
                    action.generation_id != generation_id
                    or action is self._running
                    or action.is_cleanup
                ):
                    continue
                cancelled = future is None or (
                    not future.done() and future.cancel()
                )
                if cancelled:
                    action.cancelled = True
                    if action in self._actions:
                        self._actions.remove(action)
                    action.ready.set()

    def close_admission(self, generation_id: str) -> None:
        """原子关闭代际准入，并取消尚未 submit 的预留动作。"""

        with self._lock:
            self._closed_generations.add(generation_id)
            for action in list(self._actions):
                if (
                    action.generation_id != generation_id
                    or action.is_cleanup
                    or action.future is not None
                ):
                    continue
                action.cancelled = True
                self._actions.remove(action)
                action.ready.set()

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

_PROBE_PAYLOAD_KEYS = (
    "type",
    "workflow",
    "commerceMode",
    "contentType",
    "accountId",
    "accountList",
    "fileList",
    "title",
    "description",
    "tags",
    "runtimeMode",
    "debugDryRun",
)

_CONTROLLED_SCALAR_TYPES = frozenset(
    {type(None), bool, int, float, str}
)


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
        self._state_lock = threading.RLock()
        self._action_queue = _CollectorActionQueue()
        self._runtime: _GenerationRuntime | None = None
        self._begin_flight: _LifecycleFlight | None = None
        self._manager_close_owners: dict[
            int, tuple[Any, _CollectorCloseOwner]
        ] = {}
        self._close_wait_seconds = 15.0

    def begin_generation(
        self,
        payload: Mapping[str, Any],
        *,
        on_progress=None,
    ) -> dict[str, object]:
        """关闭旧代际，建立新代际并只自动启动国内地点采集器。"""

        while True:
            with self._state_lock:
                preceding = self._begin_flight
                if preceding is None:
                    owner = _LifecycleFlight()
                    self._begin_flight = owner
                    break
            preceding.done.wait()
        try:
            return self._begin_generation(
                payload,
                begin_flight=owner,
                on_progress=on_progress,
            )
        finally:
            with self._state_lock:
                if self._begin_flight is owner:
                    self._begin_flight = None
            owner.done.set()

    def _begin_generation(
        self,
        payload: Mapping[str, Any],
        *,
        begin_flight: _LifecycleFlight,
        on_progress=None,
    ) -> dict[str, object]:

        self._require_current_begin_flight(begin_flight)
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
            self._require_current_begin_flight(begin_flight)
            if cleanup.get("closed") is not True or int(
                cleanup.get("aliveCollectorCount") or 0
            ):
                raise DouyinCommerceCollectorError("cleanup_incomplete")

        try:
            built_payload = self._probe_payload_builder(payload)
            self._require_current_begin_flight(begin_flight)
            probe_payload = self._snapshot_probe_payload(built_payload)
            self._require_current_begin_flight(begin_flight)
        except Exception:
            self._require_current_begin_flight(begin_flight)
            raise DouyinCommerceCollectorError("collector_start_failed") from None

        account_id = self._account_id(probe_payload)
        self._require_current_begin_flight(begin_flight)
        generation = new_setup_generation(account_id=account_id)
        generation.transition(SetupGenerationState.COLLECTING)
        runtime = _GenerationRuntime(
            generation=generation,
            probe_payload=probe_payload,
            collectors={},
        )
        with self._state_lock:
            self._require_current_begin_flight_locked(begin_flight)
            self._runtime = runtime
            action = self._action_queue.reserve(
                runtime.generation.generation_id,
                str(uuid4()),
                CollectorType.DOMESTIC_LOCATION,
                lambda: self._ensure_collector(
                    generation.generation_id,
                    CollectorType.DOMESTIC_LOCATION,
                    on_progress=on_progress,
                ),
            )
        self._require_current_begin_flight(begin_flight)
        self._action_queue.start(action)
        try:
            self._action_queue.wait(action)
            self._require_current_begin_flight(begin_flight)
        except DouyinCommerceCollectorError:
            self._require_current_begin_flight(begin_flight)
            raise
        except Exception:
            self._require_current_begin_flight(begin_flight)
            raise DouyinCommerceCollectorError("collector_start_failed") from None
        with self._state_lock:
            self._require_current_begin_flight_locked(begin_flight)
            return self.status(generation.generation_id)

    def _require_current_begin_flight(
        self,
        begin_flight: _LifecycleFlight,
    ) -> None:
        with self._state_lock:
            self._require_current_begin_flight_locked(begin_flight)

    def _require_current_begin_flight_locked(
        self,
        begin_flight: _LifecycleFlight,
    ) -> None:
        if begin_flight.cancelled or self._begin_flight is not begin_flight:
            raise DouyinCommerceCollectorError(
                "stale_result_discarded"
            ) from None

    def refresh_favorite_music(
        self, generation_id: str
    ) -> dict[str, object]:
        """在独立音乐会话中懒启动并刷新收藏音乐。"""

        generation_id = self._normalize_public_text(
            generation_id, error_code="stale_result_discarded"
        )
        request_id = str(uuid4())
        with self._state_lock:
            runtime = self._require_collecting_runtime(generation_id)
            current = runtime.collectors.get(CollectorType.FAVORITE_MUSIC)
            action_instance_id = (
                current.instance_id if current is not None else str(uuid4())
            )
            action = self._action_queue.reserve(
                generation_id,
                request_id,
                CollectorType.FAVORITE_MUSIC,
                lambda: self._refresh_music_action(
                    generation_id, request_id, action_instance_id
                ),
            )
        self._action_queue.start(action)
        try:
            return self._wait_public_action(
                action,
                generation_id=generation_id,
                collector_type=CollectorType.FAVORITE_MUSIC,
                request_id=request_id,
                action_name="refresh_favorite_music",
            )
        except DouyinCommerceCollectorError as error:
            raise self._contextual_action_error(
                error,
                generation_id,
                CollectorType.FAVORITE_MUSIC,
                action_instance_id,
            ) from None

    def search_locations(
        self,
        generation_id: str,
        keyword: object,
        scope: object,
    ) -> dict[str, object]:
        """按固定范围将地点搜索路由到独立会话。"""

        normalized_scope = self._normalize_public_text(
            scope, error_code="collector_scope_mismatch"
        ).strip().casefold()
        generation_id = self._normalize_public_text(
            generation_id, error_code="stale_result_discarded"
        )
        normalized_keyword = self._normalize_public_text(
            keyword, error_code="collector_unknown"
        )
        collector_type = _LOCATION_COLLECTORS.get(normalized_scope)
        if collector_type is None:
            raise DouyinCommerceCollectorError("collector_scope_mismatch")
        request_id = str(uuid4())
        with self._state_lock:
            runtime = self._require_collecting_runtime(generation_id)
            current = runtime.collectors.get(collector_type)
            action_instance_id = (
                current.instance_id if current is not None else str(uuid4())
            )
            action = self._action_queue.reserve(
                generation_id,
                request_id,
                collector_type,
                lambda: self._search_locations_action(
                    generation_id,
                    collector_type,
                    request_id,
                    normalized_keyword,
                    normalized_scope,
                    action_instance_id,
                ),
            )
        self._action_queue.start(action)
        try:
            return self._wait_public_action(
                action,
                generation_id=generation_id,
                collector_type=collector_type,
                request_id=request_id,
                action_name="search_locations",
                scope=normalized_scope,
                keyword=normalized_keyword,
            )
        except DouyinCommerceCollectorError as error:
            raise self._contextual_action_error(
                error, generation_id, collector_type, action_instance_id
            ) from None

    def retry_collector(
        self,
        generation_id: str,
        collector_type: object,
    ) -> dict[str, object]:
        """在一个队列动作中只替换指定采集器。"""

        generation_id = self._normalize_public_text(
            generation_id, error_code="stale_result_discarded"
        )
        normalized_type = self._collector_type(collector_type)
        request_id = str(uuid4())
        with self._state_lock:
            runtime = self._require_collecting_runtime(generation_id)
            current = runtime.collectors.get(normalized_type)
            expected_instance_id = current.instance_id if current else ""
            action_instance_id = str(uuid4())
            action = self._action_queue.reserve(
                generation_id,
                request_id,
                normalized_type,
                lambda: self._retry_collector_action(
                    generation_id,
                    normalized_type,
                    expected_instance_id,
                    action_instance_id,
                ),
            )
        self._action_queue.start(action)
        try:
            collector = self._wait_public_action(
                action,
                generation_id=generation_id,
                collector_type=normalized_type,
                request_id=request_id,
                action_name="retry_collector",
                scope=_FIXED_SCOPES[normalized_type],
            )
        except DouyinCommerceCollectorError as error:
            raise self._contextual_action_error(
                error, generation_id, normalized_type, action_instance_id
            ) from None
        return self._public_action_result(
            generation_id,
            normalized_type,
            collector.instance_id,
        )

    def close_generation(
        self,
        generation_id: str | None = None,
        *,
        reason: str,
    ) -> dict[str, object]:
        """幂等取消队列并尽力关闭当前代际的所有会话。"""

        generation_id = (
            None
            if generation_id is None
            else self._normalize_public_text(
                generation_id, error_code="stale_result_discarded"
            )
        )
        if generation_id == "":
            raise DouyinCommerceCollectorError("stale_result_discarded")
        normalized_reason = self._normalize_public_text(
            reason, error_code="collector_unknown"
        )
        with self._state_lock:
            if generation_id is None:
                begin_flight = self._begin_flight
                if begin_flight is not None and not begin_flight.done.is_set():
                    begin_flight.cancelled = True
                    if self._begin_flight is begin_flight:
                        self._begin_flight = None
                    begin_flight.done.set()
            runtime = self._runtime
            if runtime is None:
                return self._empty_close_result(generation_id)
            if (
                generation_id is not None
                and generation_id != runtime.generation.generation_id
            ):
                raise DouyinCommerceCollectorError("stale_result_discarded")
            existing = runtime.close_flight
            if existing is None or existing.done.is_set():
                owner = _LifecycleFlight()
                runtime.close_flight = owner
                existing = None
        if existing is not None:
            if not existing.done.wait(timeout=self._close_wait_seconds):
                with self._state_lock:
                    return self._close_result_locked(runtime)
            if existing.result is not None:
                return self._copy_close_result(existing.result)
            raise DouyinCommerceCollectorError("cleanup_incomplete")
        try:
            result = self._close_generation(
                generation_id, reason=normalized_reason
            )
        except Exception:
            owner.done.set()
            raise
        owner.result = self._copy_close_result(result)
        owner.done.set()
        return result

    def _close_generation(
        self,
        generation_id: str | None = None,
        *,
        reason: str,
    ) -> dict[str, object]:

        normalized_reason = reason.strip().casefold()
        is_publish_barrier = any(
            marker in normalized_reason
            for marker in ("publish", "preflight", "continue", "check")
        )
        cleanup_jobs: list[
            tuple[CollectorType, _CollectorRuntime, _CollectorCloseOwner]
        ] = []
        owners: list[_CollectorCloseOwner] = []
        with self._state_lock:
            runtime = self._runtime
            if runtime is None:
                return self._empty_close_result(generation_id)
            current_id = runtime.generation.generation_id
            if generation_id is not None and generation_id != current_id:
                raise DouyinCommerceCollectorError("stale_result_discarded")
            if runtime.closed_result is not None and not runtime.collectors:
                return self._copy_close_result(runtime.closed_result)
            generation = runtime.generation
            self._action_queue.close_admission(current_id)
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
            for collector_type in _COLLECTOR_ORDER:
                collector = runtime.collectors.get(collector_type)
                if collector is None:
                    runtime.cleanup_results.setdefault(
                        collector_type, "not_started"
                    )
                    continue
                slot = generation.collectors[collector_type]
                if slot.instance_id == collector.instance_id:
                    slot.state = CollectorState.CLOSING
                close_owner, claimed = self._claim_collector_close_locked(
                    collector
                )
                owners.append(close_owner)
                runtime.cleanup_results[collector_type] = "cleanup_incomplete"
                if claimed:
                    cleanup_jobs.append(
                        (collector_type, collector, close_owner)
                    )

        self._action_queue.cancel_pending(current_id)
        for collector_type, collector, close_owner in cleanup_jobs:
            def finish_collector_close(
                runtime=runtime,
                collector_type=collector_type,
                collector=collector,
                close_owner=close_owner,
            ) -> None:
                self._run_generation_collector_close(
                    runtime,
                    collector_type,
                    collector,
                    close_owner,
                )

            self._action_queue.submit(
                current_id,
                str(uuid4()),
                collector_type,
                finish_collector_close,
                is_cleanup=True,
            )

        deadline = monotonic() + self._close_wait_seconds
        for close_owner in owners:
            remaining = deadline - monotonic()
            if remaining <= 0 or not close_owner.done.wait(timeout=remaining):
                break
        with self._state_lock:
            return self._close_result_locked(runtime)

    def _run_generation_collector_close(
        self,
        runtime: _GenerationRuntime,
        collector_type: CollectorType,
        collector: _CollectorRuntime,
        owner: _CollectorCloseOwner,
    ) -> None:
        cleanup_result = "closed"
        try:
            if collector.manager is not None:
                self._close_collector_manager(
                    collector.manager,
                    collector.session_id or None,
                )
        except (KeyboardInterrupt, SystemExit):
            with self._state_lock:
                self._complete_collector_close_locked(
                    runtime,
                    collector_type,
                    collector,
                    owner,
                    "cleanup_interrupted",
                    mark_failed=True,
                )
            raise
        except Exception:
            cleanup_result = "cleanup_incomplete"

        with self._state_lock:
            self._complete_collector_close_locked(
                runtime,
                collector_type,
                collector,
                owner,
                cleanup_result,
                mark_failed=True,
            )

    def _claim_collector_close_locked(
        self,
        collector: _CollectorRuntime,
    ) -> tuple[_CollectorCloseOwner, bool]:
        existing = collector.close_owner
        if existing is not None and not existing.done.is_set():
            return existing, False
        manager = collector.manager
        if manager is not None:
            registered = self._manager_close_owners.get(id(manager))
            if (
                registered is not None
                and registered[0] is manager
                and not registered[1].done.is_set()
            ):
                collector.close_owner = registered[1]
                return registered[1], False
        owner = _CollectorCloseOwner()
        collector.close_owner = owner
        if manager is not None:
            self._manager_close_owners[id(manager)] = (manager, owner)
        return owner, True

    def _bind_manager_close_owner_locked(
        self,
        collector: _CollectorRuntime,
    ) -> None:
        """将占位期已声明的 close owner 绑定到后到的 manager 身份。"""

        manager = collector.manager
        owner = collector.close_owner
        if manager is None or owner is None or owner.done.is_set():
            return
        registered = self._manager_close_owners.get(id(manager))
        if (
            registered is not None
            and registered[0] is manager
            and not registered[1].done.is_set()
            and registered[1] is not owner
        ):
            return
        self._manager_close_owners[id(manager)] = (manager, owner)

    def _release_manager_close_owner_locked(
        self,
        collector: _CollectorRuntime,
        owner: _CollectorCloseOwner,
    ) -> None:
        manager = collector.manager
        if manager is None:
            return
        registered = self._manager_close_owners.get(id(manager))
        if (
            registered is not None
            and registered[0] is manager
            and registered[1] is owner
        ):
            self._manager_close_owners.pop(id(manager), None)

    def _complete_collector_close_locked(
        self,
        runtime: _GenerationRuntime,
        collector_type: CollectorType,
        collector: _CollectorRuntime,
        owner: _CollectorCloseOwner,
        cleanup_result: str,
        *,
        mark_failed: bool = False,
    ) -> None:
        if collector.close_owner is not owner:
            return
        owner.result = cleanup_result
        runtime.cleanup_results[collector_type] = cleanup_result
        slot = runtime.generation.collectors[collector_type]
        if cleanup_result == "closed" and slot.instance_id == collector.instance_id:
            slot.state = CollectorState.CLOSED
            slot.session_id = None
        elif mark_failed and slot.instance_id == collector.instance_id:
            slot.state = CollectorState.FAILED
        if (
            cleanup_result == "closed"
            and runtime.collectors.get(collector_type) is collector
        ):
            runtime.collectors.pop(collector_type, None)
        self._release_manager_close_owner_locked(collector, owner)
        owner.done.set()
        self._finalize_closed_runtime_locked(runtime)

    def _close_result_locked(
        self,
        runtime: _GenerationRuntime,
    ) -> dict[str, object]:
        for collector_type in _COLLECTOR_ORDER:
            runtime.cleanup_results.setdefault(collector_type, "not_started")
        alive_count = len(runtime.collectors)
        closed = alive_count == 0 and all(
            result != "cleanup_incomplete"
            for result in runtime.cleanup_results.values()
        )
        if closed:
            runtime.generation.close()
        result = {
            "closed": closed,
            "setupGenerationId": runtime.generation.generation_id,
            "aliveCollectorCount": alive_count,
            "cleanupResults": {
                collector_type.value: runtime.cleanup_results[collector_type]
                for collector_type in _COLLECTOR_ORDER
            },
        }
        if closed:
            runtime.closed_result = self._copy_close_result(result)
        return result

    def _finalize_closed_runtime_locked(self, runtime: _GenerationRuntime) -> None:
        if runtime.collectors:
            return
        if runtime.generation.state not in {
            SetupGenerationState.CANCELLING,
            SetupGenerationState.CLOSING_COLLECTORS,
        }:
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

        generation_id = (
            None
            if generation_id is None
            else self._normalize_public_text(
                generation_id, error_code="stale_result_discarded"
            )
        )
        if generation_id == "":
            raise DouyinCommerceCollectorError("stale_result_discarded")
        with self._state_lock:
            runtime = self._runtime
            if runtime is None:
                return {
                    "setupGenerationId": (
                        generation_id if generation_id is not None else ""
                    ),
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
            if (
                generation_id is not None
                and generation_id != runtime.generation.generation_id
            ):
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

    def _refresh_music_action(
        self,
        generation_id: str,
        request_id: str,
        action_instance_id: str,
    ) -> dict[str, object]:
        collector = self._ensure_collector(
            generation_id,
            CollectorType.FAVORITE_MUSIC,
            action_instance_id=action_instance_id,
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
        return self._public_action_result(
            generation_id,
            CollectorType.FAVORITE_MUSIC,
            collector.instance_id,
            candidates=public_result,
        )

    def _search_locations_action(
        self,
        generation_id: str,
        collector_type: CollectorType,
        request_id: str,
        keyword: object,
        scope: str,
        action_instance_id: str,
    ) -> dict[str, object]:
        collector = self._ensure_collector(
            generation_id,
            collector_type,
            action_instance_id=action_instance_id,
        )
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
        return self._public_action_result(
            generation_id,
            collector_type,
            collector.instance_id,
            candidates=public_result,
        )

    def _retry_collector_action(
        self,
        generation_id: str,
        collector_type: CollectorType,
        expected_instance_id: str,
        action_instance_id: str,
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
            with self._state_lock:
                slot = runtime.generation.collectors[collector_type]
                owns_current_slot = bool(
                    self._runtime is runtime
                    and runtime.generation.generation_id == generation_id
                    and runtime.generation.state
                    in {
                        SetupGenerationState.COLLECTING,
                        SetupGenerationState.READY,
                    }
                    and runtime.collectors.get(collector_type) is old
                    and slot.instance_id == old.instance_id
                    and slot.state is CollectorState.RETRYING
                )
                if not owns_current_slot:
                    raise DouyinCommerceCollectorError(
                        "stale_result_discarded"
                    )
                owner, claimed = self._claim_collector_close_locked(old)
            if not claimed:
                raise DouyinCommerceCollectorError("stale_result_discarded")
            cleanup_result = "closed"
            try:
                if old.manager is not None:
                    self._close_collector_manager(
                        old.manager,
                        old.session_id or None,
                    )
            except Exception:
                cleanup_result = "cleanup_incomplete"
            with self._state_lock:
                owner.result = cleanup_result
                self._release_manager_close_owner_locked(old, owner)
                owner.done.set()
                slot = runtime.generation.collectors[collector_type]
                still_owns_current_slot = bool(
                    self._runtime is runtime
                    and runtime.generation.generation_id == generation_id
                    and runtime.generation.state
                    in {
                        SetupGenerationState.COLLECTING,
                        SetupGenerationState.READY,
                    }
                    and runtime.collectors.get(collector_type) is old
                    and slot.instance_id == old.instance_id
                    and slot.state is CollectorState.RETRYING
                )
                if cleanup_result != "closed":
                    if not still_owns_current_slot:
                        raise DouyinCommerceCollectorError(
                            "stale_result_discarded"
                        ) from None
                    slot.state = CollectorState.FAILED
                    runtime.cleanup_results[collector_type] = (
                        "cleanup_incomplete"
                    )
                    raise DouyinCommerceCollectorError(
                        "cleanup_incomplete",
                        generation_id=generation_id,
                        collector_type=collector_type.value,
                        collector_instance_id=expected_instance_id,
                    ) from None
                if not still_owns_current_slot:
                    if (
                        self._runtime is runtime
                        and runtime.collectors.get(collector_type) is old
                        and slot.instance_id == old.instance_id
                    ):
                        runtime.cleanup_results[collector_type] = "closed"
                        slot.state = CollectorState.CLOSED
                        slot.session_id = None
                        runtime.collectors.pop(collector_type, None)
                        self._finalize_closed_runtime_locked(runtime)
                    raise DouyinCommerceCollectorError(
                        "stale_result_discarded"
                    ) from None
                runtime.collectors.pop(collector_type, None)
                runtime.cleanup_results.pop(collector_type, None)
                slot.instance_id = None
                slot.session_id = None
                slot.state = CollectorState.RETRYING

        return self._ensure_collector(
            generation_id,
            collector_type,
            action_instance_id=action_instance_id,
        )

    def _ensure_collector(
        self,
        generation_id: str,
        collector_type: CollectorType,
        *,
        on_progress=None,
        action_instance_id: str = "",
    ) -> _CollectorRuntime:
        with self._state_lock:
            runtime = self._require_collecting_runtime(generation_id)
            existing = runtime.collectors.get(collector_type)
            if existing is not None:
                if (
                    action_instance_id
                    and existing.instance_id != action_instance_id
                ):
                    raise DouyinCommerceCollectorError(
                        "stale_result_discarded"
                    )
                if existing.fixed_scope != _FIXED_SCOPES[collector_type]:
                    raise DouyinCommerceCollectorError("collector_scope_mismatch")
                self._validate_active_collector(generation_id, existing)
                return existing

            collector = _CollectorRuntime(
                collector_type=collector_type,
                manager=None,
                instance_id=action_instance_id or str(uuid4()),
                fixed_scope=_FIXED_SCOPES[collector_type],
            )
            runtime.collectors[collector_type] = collector
            slot = runtime.generation.collectors[collector_type]
            slot.state = CollectorState.STARTING
            slot.instance_id = collector.instance_id
            slot.session_id = None
            stored_probe_payload = runtime.probe_payload

        try:
            probe_payload = self._rebuild_controlled_value(stored_probe_payload)
            if type(probe_payload) is not dict:
                raise TypeError("probe payload snapshot is invalid")
        except Exception:
            if not self._mark_failed(generation_id, collector):
                raise DouyinCommerceCollectorError(
                    "stale_result_discarded"
                ) from None
            raise DouyinCommerceCollectorError("collector_start_failed") from None

        try:
            manager = self._manager_factory()
        except Exception:
            if not self._mark_failed(generation_id, collector):
                raise DouyinCommerceCollectorError(
                    "stale_result_discarded"
                ) from None
            raise DouyinCommerceCollectorError("collector_start_failed") from None

        with self._state_lock:
            current = self._runtime
            slot = runtime.generation.collectors[collector_type]
            can_start = bool(
                current is runtime
                and current.generation.generation_id == generation_id
                and current.generation.state
                in {SetupGenerationState.COLLECTING, SetupGenerationState.READY}
                and current.collectors.get(collector_type) is collector
                and slot.instance_id == collector.instance_id
                and slot.state is CollectorState.STARTING
            )
            manager_is_shared = any(
                item is not collector and item.manager is manager
                for item in runtime.collectors.values()
            )
            if not manager_is_shared:
                collector.manager = manager
                self._bind_manager_close_owner_locked(collector)
            has_close_owner = bool(
                collector.close_owner is not None
                and not collector.close_owner.done.is_set()
            )
        if manager_is_shared:
            self._reject_collector_collision(
                runtime,
                collector,
                close_rejected_manager=False,
            )
            raise DouyinCommerceCollectorError(
                "collector_start_failed" if can_start else "stale_result_discarded"
            ) from None
        if not can_start:
            if not has_close_owner:
                self._discard_unaccepted_start(runtime, collector)
            raise DouyinCommerceCollectorError("stale_result_discarded")

        try:
            started = manager.start_upload(probe_payload, on_progress=on_progress)
            if not isinstance(started, Mapping) or not started:
                raise TypeError("collector start result is invalid")
            if str(started.get("status") or "").strip().casefold() == "needs_login":
                raise DouyinCommerceCollectorError("login_required")
            raw_session_id = started.get("sessionId")
            if not raw_session_id:
                raise TypeError("collector session id is missing")
            session_id = str(raw_session_id).strip()
            if not session_id:
                raise TypeError("collector session id is empty")
        except Exception as error:
            if (
                isinstance(error, DouyinCommerceCollectorError)
                and error.code == "login_required"
            ):
                self._mark_failed(generation_id, collector)
                raise DouyinCommerceCollectorError("login_required") from None
            if not self._mark_failed(generation_id, collector):
                raise DouyinCommerceCollectorError(
                    "stale_result_discarded"
                ) from None
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
            has_close_owner = bool(
                collector.close_owner is not None
                and not collector.close_owner.done.is_set()
            )
            session_is_shared = any(
                item is not collector and item.session_id == session_id
                for item in runtime.collectors.values()
            )
            if can_activate and not session_is_shared:
                runtime.generation.activate_collector(
                    collector_type,
                    instance_id=collector.instance_id,
                    session_id=session_id,
                )
                return collector
            if can_activate:
                slot.state = CollectorState.FAILED
        if not can_activate:
            if not has_close_owner:
                self._discard_unaccepted_start(runtime, collector)
            raise DouyinCommerceCollectorError("stale_result_discarded")
        self._reject_collector_collision(
            runtime,
            collector,
            close_rejected_manager=True,
        )
        raise DouyinCommerceCollectorError("collector_start_failed")

    def _reject_collector_collision(
        self,
        runtime: _GenerationRuntime,
        collector: _CollectorRuntime,
        *,
        close_rejected_manager: bool,
    ) -> None:
        collector_type = collector.collector_type
        if not close_rejected_manager:
            with self._state_lock:
                if runtime.collectors.get(collector_type) is collector:
                    runtime.collectors.pop(collector_type, None)
                slot = runtime.generation.collectors[collector_type]
                if slot.instance_id == collector.instance_id:
                    slot.state = CollectorState.FAILED
                    slot.instance_id = None
                    slot.session_id = None
            return

        with self._state_lock:
            owner, claimed = self._claim_collector_close_locked(collector)
        if not claimed:
            # generation close 已拥有该 manager；其 cleanup action 排在
            # 当前碰撞动作之后。此处不得伪造完成或移除 runtime。
            return
        cleanup_result = "closed"
        try:
            if collector.manager is not None:
                self._close_collector_manager(collector.manager, None)
        except Exception:
            cleanup_result = "cleanup_incomplete"
        with self._state_lock:
            if collector.close_owner is not owner:
                return
            owner.result = cleanup_result
            runtime.cleanup_results[collector_type] = cleanup_result
            slot = runtime.generation.collectors[collector_type]
            if cleanup_result == "closed":
                if runtime.collectors.get(collector_type) is collector:
                    runtime.collectors.pop(collector_type, None)
                runtime.cleanup_results.pop(collector_type, None)
                if slot.instance_id == collector.instance_id:
                    slot.state = CollectorState.FAILED
                    slot.instance_id = None
                    slot.session_id = None
            elif slot.instance_id == collector.instance_id:
                slot.state = CollectorState.FAILED
            self._release_manager_close_owner_locked(collector, owner)
            owner.done.set()
            self._finalize_closed_runtime_locked(runtime)

    def _discard_unaccepted_start(
        self,
        runtime: _GenerationRuntime,
        collector: _CollectorRuntime,
    ) -> None:
        with self._state_lock:
            owner, claimed = self._claim_collector_close_locked(collector)
        if not claimed:
            return
        cleanup_result = "closed"
        try:
            if collector.manager is not None:
                self._close_collector_manager(
                    collector.manager,
                    collector.session_id or None,
                )
        except Exception:
            cleanup_result = "cleanup_incomplete"
        with self._state_lock:
            self._complete_collector_close_locked(
                runtime,
                collector.collector_type,
                collector,
                owner,
                cleanup_result,
            )

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
        keyword: str = "",
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
        keyword: str = "",
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

    def _public_action_result(
        self,
        generation_id: str,
        collector_type: CollectorType,
        collector_instance_id: str,
        *,
        candidates: list[dict[str, Any]] | None = None,
    ) -> dict[str, object]:
        """返回带代际、类型、实例三重门禁的统一动作结果。"""

        status = self.status(generation_id)
        instances = status.get("collectorInstanceIds")
        current_instance_id = (
            str(instances.get(collector_type.value) or "")
            if isinstance(instances, Mapping)
            else ""
        )
        if not collector_instance_id or current_instance_id != collector_instance_id:
            raise DouyinCommerceCollectorError(
                "stale_result_discarded",
                generation_id=generation_id,
                collector_type=collector_type.value,
                collector_instance_id=collector_instance_id,
            ) from None
        result: dict[str, object] = {
            **status,
            "ok": True,
            "collectorType": collector_type.value,
            "collectorInstanceId": collector_instance_id,
        }
        if candidates is not None:
            result["candidates"] = [dict(item) for item in candidates]
        return result

    def _contextual_action_error(
        self,
        error: DouyinCommerceCollectorError,
        generation_id: str,
        collector_type: CollectorType,
        action_instance_id: str,
    ) -> DouyinCommerceCollectorError:
        """使用动作派发时捕获的不可变实例上下文投影固定错误。"""

        return DouyinCommerceCollectorError(
            error.code,
            event_emitted=error.event_emitted,
            generation_id=generation_id,
            collector_type=collector_type.value,
            collector_instance_id=(
                error.collector_instance_id or action_instance_id
            ),
        )

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
            or runtime.generation.generation_id != generation_id
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
        keyword: str,
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
            keyword=keyword,
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
            normalized = "" if value is None else str(value).strip().casefold()
            return CollectorType(normalized)
        except Exception:
            raise DouyinCommerceCollectorError("collector_unknown") from None

    @staticmethod
    def _normalize_public_text(value: object, *, error_code: str) -> str:
        try:
            return "" if value is None else str(value)
        except Exception:
            raise DouyinCommerceCollectorError(error_code) from None

    @staticmethod
    def _close_collector_manager(
        manager: Any,
        session_id: str | None,
    ) -> None:
        strict_close = getattr(manager, "close_strict", None)
        if callable(strict_close):
            strict_close(session_id)
            return
        manager.close(session_id)

    @classmethod
    def _snapshot_probe_payload(cls, payload: object) -> dict[str, Any]:
        if type(payload) is not dict:
            raise TypeError("probe payload must be a controlled mapping")
        outer = payload
        whitelisted = {
            key: outer[key]
            for key in _PROBE_PAYLOAD_KEYS
            if key in outer
        }
        snapshot = cls._rebuild_controlled_value(whitelisted)
        if type(snapshot) is not dict:
            raise TypeError("probe payload snapshot is invalid")
        return snapshot

    @classmethod
    def _rebuild_controlled_value(
        cls,
        value: object,
        active_containers: set[int] | None = None,
    ) -> object:
        value_type = type(value)
        if value_type in _CONTROLLED_SCALAR_TYPES:
            return value
        if value_type not in {list, tuple, dict}:
            raise TypeError("probe payload value is not controlled")

        active = active_containers if active_containers is not None else set()
        marker = id(value)
        if marker in active:
            raise TypeError("probe payload contains a cycle")
        active.add(marker)
        try:
            if value_type is list:
                return [
                    cls._rebuild_controlled_value(item, active)
                    for item in value
                ]
            if value_type is tuple:
                return tuple(
                    cls._rebuild_controlled_value(item, active)
                    for item in value
                )

            rebuilt: dict[object, object] = {}
            for key, item in value.items():
                rebuilt_key = cls._rebuild_controlled_key(key, active)
                rebuilt[rebuilt_key] = cls._rebuild_controlled_value(
                    item, active
                )
            return rebuilt
        finally:
            active.remove(marker)

    @classmethod
    def _rebuild_controlled_key(
        cls,
        key: object,
        active_containers: set[int],
    ) -> object:
        key_type = type(key)
        if key_type in _CONTROLLED_SCALAR_TYPES:
            return key
        if key_type is not tuple:
            raise TypeError("probe payload key is not controlled")

        marker = id(key)
        if marker in active_containers:
            raise TypeError("probe payload key contains a cycle")
        active_containers.add(marker)
        try:
            return tuple(
                cls._rebuild_controlled_key(item, active_containers)
                for item in key
            )
        finally:
            active_containers.remove(marker)

    @staticmethod
    def _account_id(payload: Mapping[str, Any]) -> int:
        try:
            value: object = payload.get("accountId", 0)
            if value is None or value == 0 or value == "":
                accounts = payload.get("accountList")
                if (
                    isinstance(accounts, list)
                    and accounts
                    and isinstance(accounts[0], Mapping)
                ):
                    value = accounts[0].get("id", 0)
            return int(value)
        except Exception:
            raise DouyinCommerceCollectorError(
                "collector_start_failed"
            ) from None

    @staticmethod
    def _empty_close_result(generation_id: str | None) -> dict[str, object]:
        return {
            "closed": True,
            "setupGenerationId": generation_id if generation_id is not None else "",
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
