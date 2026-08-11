# -*- coding: utf-8 -*-
"""抖音带货批量任务的串行、无头会话协调器。

本模块不提供任何可直接传入字典写成功的公开桥接。成功只会在执行器从同一
submit 会话取得最终平台回读后，经内部写入器落入任务记录；预检、编辑页
字段写入、验证码等待和本地排期都不能被写成平台成功。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
import re
import threading
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from . import task_service
from ._douyin_commerce_batch_receipt_writer import _write_final_batch_receipt
from .douyin_commerce_batch_service import (
    SHANGHAI_TIMEZONE,
    apply_interval_schedule,
    item_publish_payload,
    validate_batch_payload,
)
from .douyin_commerce_session import commerce_session_manager
from .douyin_location_preset_service import (
    DouyinLocationPresetError,
    match_location_preset,
)
from .douyin_sms_cooldown import DouyinSmsCooldownError, DouyinSmsCooldownGate
from .douyin_verification import (
    DouyinVerificationError,
    VerificationChallenge,
    verification_broker as _default_verification_broker,
)
from utils.log import douyin_logger


_SHANGHAI = ZoneInfo(SHANGHAI_TIMEZONE)
_INTERVENTION_MARKERS = ("验证", "验证码", "二维码", "登录", "风控", "合规", "未知")
_LOGIN_PAUSE_STATUSES = frozenset(
    {"needs_login", "login_required", "requires_login", "login_expired"}
)
_ACTIVE_VERIFICATION_STATES = frozenset({"waiting", "processing"})
_AMBIGUOUS_SUBMIT_MARKERS = (
    "最终提交未能获得平台回执",
    "定时提交后未能从作品管理页回读",
    "点击发布后未确认跳转",
    "点击发布后 5 分钟内未进入作品管理页",
)


class DouyinCommerceBatchExecutorError(RuntimeError):
    """批量带货任务不能安全继续。"""


@dataclass(frozen=True)
class BatchProgressEvent:
    """仅供客户端显示的逐条非敏感进度。"""

    index: int
    total: int
    phase: str
    message: str
    remaining_seconds: int | None = None

    def to_public_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "index": self.index,
            "total": self.total,
            "phase": self.phase,
            "message": self.message,
        }
        if type(self.remaining_seconds) is int:
            result["remainingSeconds"] = self.remaining_seconds
        return result


def _text(value: object) -> str:
    return " ".join(str(value or "").replace("\u200b", " ").split())


def _safe_item_label(item: Mapping[str, Any], index: int) -> str:
    name = Path(_text(item.get("mediaPath"))).name
    return name or f"第 {index + 1} 条视频"


def _location_search_keywords(
    location: Mapping[str, Any],
    *,
    original_keyword: object = "",
) -> list[str]:
    """返回地点恢复时的有序检索词，并优先复用用户的真实搜索意图。"""

    address = _text(location.get("address"))
    name = _text(location.get("name"))
    original = _text(original_keyword)
    result: list[str] = []

    def append(value: object) -> None:
        normalized = _text(value)
        if normalized and normalized not in result:
            result.append(normalized)

    append(original)
    # 旧任务没有 searchKeyword。对于 JOYMARK(株洲王府星mall店) 这类
    # 中英文混排名称，平台通常只接受短品牌词，完整名称和长地址均可能无结果。
    brand_tokens = [
        token
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9._-]{1,}", name)
        if token.casefold() not in {"mall", "store", "shop"}
    ]
    brand = brand_tokens[0] if brand_tokens else ""
    append(brand)

    city_source = address
    if "自治区" in city_source:
        city_source = city_source.split("自治区", 1)[1]
    elif "省" in city_source:
        city_source = city_source.split("省", 1)[1]
    city_match = re.search(r"([\u4e00-\u9fff]{2,}市)", city_source)
    city = _text(city_match.group(1)[:-1]) if city_match else ""
    if original or brand:
        append(name)
        append(address)
        append(f"{city} {name}" if city and name else "")
    else:
        append(address)
        append(f"{city} {name}" if city and name else "")
        append(name)
    return result


_PUBLIC_BATCH_DIAGNOSTICS = {
    "publish_location_candidate_missing": (
        "发布定位恢复失败：正式发布页按已保存范围和搜索词重新搜索后，"
        "没有找到与预设 POI 完全一致的地点"
        "（错误码 publish_location_candidate_missing；请查看执行日志中的逐关键词结果）"
    ),
    "publish_location_candidate_ambiguous": (
        "发布定位恢复失败：正式发布页返回了多个无法唯一确认的同名地点"
        "（错误码 publish_location_candidate_ambiguous）"
    ),
    "publish_location_click_failed": (
        "发布定位恢复失败：已找到目标地点，但平台候选无法安全点击"
        "（错误码 publish_location_click_failed）"
    ),
    "publish_location_readback_mismatch": (
        "发布定位恢复失败：点击地点后，平台回读与已保存地点不一致"
        "（错误码 publish_location_readback_mismatch）"
    ),
    "publish_location_cleanup_incomplete": (
        "发布定位恢复失败：地点候选面板未能安全关闭"
        "（错误码 publish_location_cleanup_incomplete）"
    ),
}


def _public_batch_diagnostic(value: object) -> str:
    diagnostic = _text(value)
    return _PUBLIC_BATCH_DIAGNOSTICS.get(diagnostic, diagnostic)


def _is_intervention_error(error: Exception) -> bool:
    if isinstance(error, DouyinVerificationError):
        return True
    message = _text(error).casefold()
    return any(marker in message for marker in _INTERVENTION_MARKERS)


def _controlled_upload_pause_status(upload: object) -> str:
    """只识别会话管理器明确返回的登录失效状态，避免猜测异常文本。"""

    if not isinstance(upload, Mapping):
        return ""
    status = _text(upload.get("status")).casefold().replace("-", "_").replace(" ", "_")
    return "waiting_login" if status in _LOGIN_PAUSE_STATUSES else ""


def _is_ambiguous_submit_error(error: Exception) -> bool:
    """最终点击已发生但缺回执时，只能暂停核对，不得继续提交。"""

    message = _text(error)
    return any(marker in message for marker in _AMBIGUOUS_SUBMIT_MARKERS)


def _runtime_payload(
    batch: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    mode: str,
) -> dict[str, Any]:
    """为单条会话补齐严格运行模式，且不污染持久化批次。"""

    payload = item_publish_payload(batch, item)
    payload["runtimeMode"] = mode
    payload["debugDryRun"] = mode == "preflight"
    return payload


def _final_receipt_from_submit_result(
    result: object,
    *,
    expected_schedule_time: str,
) -> tuple[str, dict[str, str], str]:
    """只接受会话 submit 返回的最终管理页回读，不补造日期或时区。"""

    if not isinstance(result, Mapping) or result.get("ok") is not True:
        raise DouyinCommerceBatchExecutorError("抖音批量执行器缺少成功的平台最终结果")
    message = _text(result.get("message"))
    if not message:
        raise DouyinCommerceBatchExecutorError("抖音批量执行器缺少平台最终回读说明")

    if result.get("scheduled") is True:
        if not expected_schedule_time:
            raise DouyinCommerceBatchExecutorError(
                "抖音立即发布任务却返回了定时回读，发布已安全停止"
            )
        readback = result.get("scheduledReadback")
        if not isinstance(readback, Mapping):
            raise DouyinCommerceBatchExecutorError("抖音定时提交缺少平台管理页回读")
        receipt = {
            "scheduleTime": _text(readback.get("scheduledAt")),
            "timezone": _text(readback.get("timezone")),
        }
        if receipt["scheduleTime"] != expected_schedule_time:
            raise DouyinCommerceBatchExecutorError(
                "抖音定时提交平台回读时间与当前视频设定时间不一致"
            )
        return "platform_scheduled_receipt", receipt, message

    if expected_schedule_time:
        raise DouyinCommerceBatchExecutorError(
            "抖音定时提交未返回平台定时回读，发布已安全停止"
        )

    readback = result.get("platformReceipt")
    if not isinstance(readback, Mapping):
        raise DouyinCommerceBatchExecutorError("抖音发表缺少平台最终回读")
    receipt = {
        key: value
        for key, value in (
            ("platformPostId", _text(readback.get("platformPostId"))),
            # 抖音即时发表回执会返回管理页 url；它是平台最终跳转的真实证据，
            # 不把它伪造为作品 ID 或发布时间。
            ("postUrl", _text(readback.get("postUrl") or readback.get("url"))),
            ("publishedAt", _text(readback.get("publishedAt"))),
            ("timezone", _text(readback.get("timezone")) or SHANGHAI_TIMEZONE),
        )
        if value
    }
    return "platform_publish_receipt", receipt, message


class DouyinCommerceBatchExecutor:
    """以一个编辑会话处理一条视频的批量带货执行器。

    不会自行创建任务、读取账号凭据或启动浏览器。浏览器仅在调用方显式执行
    run_preflight 或 run_publish(confirmed=True) 时由注入会话管理器创建；
    离线测试使用内存替身。
    """

    def __init__(
        self,
        manager: Any = commerce_session_manager,
        *,
        task_store: Any = task_service,
        now: Callable[[], datetime] | None = None,
        verification_broker: Any = _default_verification_broker,
        cooldown_gate: DouyinSmsCooldownGate | None = None,
        utc_now: Callable[[], datetime] | None = None,
    ) -> None:
        self._manager = manager
        self._task_store = task_store
        self._now = now or (lambda: datetime.now(_SHANGHAI))
        self._verification_broker = verification_broker
        self._cooldown_gate = cooldown_gate or DouyinSmsCooldownGate()
        self._utc_now = utc_now or (lambda: datetime.now(timezone.utc))
        self._pause_requested = threading.Event()
        self._shutdown_requested = threading.Event()
        self._run_lock = threading.RLock()
        self._run_condition = threading.Condition(self._run_lock)
        self._run_generation = 0
        self._claimed_submit_items: set[tuple[int, int, int]] = set()
        self._active_submit_leases: set[tuple[int, int, int]] = set()
        self._active_resend_observer_leases = 0

    def request_pause(self, *, source: str = "") -> bool:
        """只接受客户端明确确认的暂停请求，不把验证事件误当成人工暂停。"""

        if _text(source) != "user_confirmed":
            return False
        if self._pause_requested.is_set():
            return False
        self._pause_requested.set()
        return True

    def _reset_pause_request(self) -> None:
        self._pause_requested.clear()

    def reset_shutdown(self) -> int:
        """为新 worker 建立唯一运行代际，并清除之前的客户端退出标记。"""

        with self._run_condition:
            while (
                self._active_submit_leases
                or self._active_resend_observer_leases
            ):
                self._run_condition.wait()
            self._run_generation += 1
            self._shutdown_requested.clear()
            self._claimed_submit_items.clear()
            return self._run_generation

    def request_shutdown(self, source: str = "") -> bool:
        """只接受客户端退出请求，不允许其他来源中断提交。"""

        if _text(source) != "client_shutdown":
            return False
        with self._run_lock:
            if self._shutdown_requested.is_set():
                return False
            self._shutdown_requested.set()
            return True

    def _run_was_cancelled(self, run_generation: int) -> bool:
        with self._run_lock:
            return (
                self._shutdown_requested.is_set()
                or run_generation != self._run_generation
            )

    def run_preflight(
        self,
        batch: Mapping[str, Any],
        *,
        task_id: int,
        progress: Callable[[BatchProgressEvent], None] | None = None,
    ) -> list[dict[str, object]]:
        """逐条写入并回读编辑器字段；绝不调用 submit。"""

        self._reset_pause_request()
        return self._run(
            self._prepare_batch(batch),
            task_id=task_id,
            publish=False,
            progress=progress,
            run_generation=self._run_generation,
        )

    def run_publish(
        self,
        batch: Mapping[str, Any],
        *,
        task_id: int,
        confirmed: bool = False,
        progress: Callable[[BatchProgressEvent], None] | None = None,
    ) -> list[dict[str, object]]:
        """在调用方明确完成总确认后，串行提交每条视频。"""

        if confirmed is not True:
            raise DouyinCommerceBatchExecutorError("抖音带货批量发布必须先完成总确认")
        with self._run_lock:
            run_generation = self._run_generation
        prepared = self._prepare_batch(batch)
        account_key = _text(prepared.get("accountFile"))
        try:
            remaining = self._task_store.load_douyin_sms_cooldown_remaining(
                task_id, now_utc=self._utc_now()
            )
            self._cooldown_gate.restore_remaining(account_key, remaining)
        except Exception:
            raise DouyinCommerceBatchExecutorError(
                "verification_cooldown_state_invalid"
            ) from None
        self._reset_pause_request()
        return self._run(
            prepared,
            task_id=task_id,
            publish=True,
            progress=progress,
            run_generation=run_generation,
        )

    def _prepare_batch(self, batch: Mapping[str, Any]) -> dict[str, Any]:
        checked = validate_batch_payload(batch)
        return apply_interval_schedule(checked, now=self._now())

    def _task_item_ids(self, task_id: int, expected_count: int) -> list[int]:
        task = self._task_store.get_task(int(task_id))
        if not isinstance(task, Mapping):
            raise DouyinCommerceBatchExecutorError("抖音带货批量任务不存在")
        items = task.get("items")
        if not isinstance(items, list) or len(items) != expected_count:
            raise DouyinCommerceBatchExecutorError("抖音带货批量任务条目与当前批次不一致")
        result: list[int] = []
        for item in items:
            try:
                item_id = int(item.get("id"))
            except (AttributeError, TypeError, ValueError) as exc:
                raise DouyinCommerceBatchExecutorError("抖音带货批量任务缺少视频条目") from exc
            if item_id <= 0:
                raise DouyinCommerceBatchExecutorError("抖音带货批量任务缺少视频条目")
            result.append(item_id)
        return result

    @staticmethod
    def _emit(
        progress: Callable[[BatchProgressEvent], None] | None,
        *,
        index: int,
        total: int,
        phase: str,
        message: str,
        remaining_seconds: int | None = None,
    ) -> None:
        if progress is None:
            return
        try:
            progress(
                BatchProgressEvent(
                    index,
                    total,
                    phase,
                    message,
                    remaining_seconds,
                )
            )
        except Exception:
            return

    def _record_progress(
        self,
        task_id: int,
        item_id: int,
        *,
        ok: bool,
        event_type: str,
        message: str,
    ) -> None:
        self._task_store.mark_batch_item_result(
            int(task_id),
            int(item_id),
            ok=ok,
            event_type=event_type,
            message=message,
            readback={},
        )

    def _record_final_submit_result(
        self,
        task_id: int,
        item_id: int,
        result: object,
        *,
        expected_schedule_time: str,
    ) -> None:
        """执行器唯一的成功写入点：只消费本次 submit 最终回读。"""

        event_type, receipt, message = _final_receipt_from_submit_result(
            result,
            expected_schedule_time=expected_schedule_time,
        )
        try:
            _write_final_batch_receipt(
                int(task_id),
                int(item_id),
                event_type=event_type,
                readback=receipt,
                timezone=receipt.get("timezone", ""),
                message=message,
            )
        except ValueError as exc:
            raise DouyinCommerceBatchExecutorError(str(exc)) from exc

    def _active_verification_request_id(self, task_id: int) -> str:
        """返回仍由同一进程 broker 持有的 active 请求标识。

        验证被取消、过期或失败后，broker 不会再返回 active 请求。此时绝不能
        用异常文本把失败伪装成可恢复的 ``waiting_verification``。
        """

        try:
            request_id = self._verification_broker.request_for_task(int(task_id))
            if not request_id:
                return ""
            snapshot = self._verification_broker.snapshot(request_id)
        except Exception:
            return ""
        if _text(snapshot.get("state")) not in _ACTIVE_VERIFICATION_STATES:
            return ""
        return _text(request_id)

    def _has_active_verification(self, task_id: int) -> bool:
        return bool(self._active_verification_request_id(task_id))

    def _record_login_waiting(
        self,
        task_id: int,
        item_id: int,
        *,
        index: int,
        total: int,
        progress: Callable[[BatchProgressEvent], None] | None,
    ) -> dict[str, object]:
        self._record_progress(
            task_id,
            item_id,
            ok=True,
            event_type="login_waiting",
            message=f"第 {index + 1} 条视频需要到账号管理重新登录，批量已暂停",
        )
        self._emit(
            progress,
            index=index,
            total=total,
            phase="waiting_login",
            message=f"第 {index + 1} 条视频等待重新登录",
        )
        return {"index": index, "status": "waiting_login"}

    def _record_active_verification_waiting(
        self,
        task_id: int,
        item_id: int,
        *,
        index: int,
        total: int,
        progress: Callable[[BatchProgressEvent], None] | None,
    ) -> dict[str, object]:
        self._record_progress(
            task_id,
            item_id,
            ok=True,
            event_type="verification_waiting",
            message=f"第 {index + 1} 条视频需要用户完成抖音验证，批量已暂停",
        )
        self._emit(
            progress,
            index=index,
            total=total,
            phase="waiting_verification",
            message=f"第 {index + 1} 条视频等待用户验证",
        )
        return {"index": index, "status": "waiting_verification"}

    def _verification_progress_callback(
        self,
        *,
        task_id: int,
        item_id: int,
        index: int,
        total: int,
        label: str,
        account_key: str,
        run_generation: int,
        progress: Callable[[BatchProgressEvent], None] | None,
        error_sentinel: list[str] | None = None,
    ) -> Callable[[object], object]:
        """为原生验证弹窗附上内存条目上下文，不把挑战对象写入任务记录。"""

        first_request_id = ""
        handled = False
        sentinel = error_sentinel if error_sentinel is not None else []

        def _remember_error(code: str) -> None:
            if not sentinel:
                sentinel.append(code)

        def _record_sms_trigger() -> None:
            try:
                self._cooldown_gate.record_trigger(account_key)
            except DouyinSmsCooldownError as exc:
                _remember_error(_text(exc))
                return
            except Exception:
                _remember_error("verification_cooldown_failed")
                return
            try:
                self._task_store.record_douyin_sms_cooldown(
                    task_id,
                    item_id,
                    triggered_at_utc=self._utc_now(),
                )
            except Exception:
                _remember_error("verification_cooldown_failed")

        def _resend_confirmed(confirmed_request_id: str) -> None:
            with self._run_condition:
                if (
                    self._shutdown_requested.is_set()
                    or run_generation != self._run_generation
                    or _text(confirmed_request_id) != first_request_id
                ):
                    return
                self._active_resend_observer_leases += 1
            try:
                _record_sms_trigger()
            finally:
                with self._run_condition:
                    self._active_resend_observer_leases -= 1
                    self._run_condition.notify_all()

        def _callback(challenge: object) -> object:
            nonlocal first_request_id, handled
            if isinstance(challenge, VerificationChallenge):
                # 这份副本只存在回调栈中；二维码字节仍由 broker 在内存托管。
                challenge = replace(challenge, item_index=index, item_label=label)
            else:
                return challenge
            # session manager 仅会在 broker 请求已经创建后调用回调。没有 active
            # 请求时不落 waiting 事件，以免已取消/过期的验证码留下可恢复假象。
            with self._run_lock:
                if (
                    self._shutdown_requested.is_set()
                    or run_generation != self._run_generation
                ):
                    return challenge
                active_request_id = self._active_verification_request_id(task_id)
                if not active_request_id:
                    return challenge
                if not first_request_id:
                    first_request_id = active_request_id
                if (
                    handled
                    or active_request_id != first_request_id
                ):
                    return challenge
                handled = True
            if (
                challenge.kind == "sms"
            ):
                try:
                    register_observer = getattr(
                        self._verification_broker,
                        "register_sms_resend_confirmed_observer",
                        None,
                    )
                    if callable(register_observer):
                        register_observer(first_request_id, _resend_confirmed)
                except Exception:
                    _remember_error("verification_cooldown_failed")
                    return challenge
                _record_sms_trigger()
                if sentinel:
                    return challenge
            try:
                self._record_active_verification_waiting(
                    task_id,
                    item_id,
                    index=index,
                    total=total,
                    progress=progress,
                )
            except Exception:
                _remember_error("verification_cooldown_failed")
            return challenge

        return _callback

    def _wait_for_sms_cooldown(
        self,
        account_key: str,
        *,
        task_id: int,
        item_id: int,
        index: int,
        total: int,
        progress: Callable[[BatchProgressEvent], None] | None,
        run_generation: int,
    ) -> bool:
        cancelled = lambda: self._run_was_cancelled(run_generation)
        try:
            if cancelled():
                return False
            if self._cooldown_gate.remaining_seconds(account_key) <= 0:
                return True
            try:
                self._record_progress(
                    task_id,
                    item_id,
                    ok=True,
                    event_type="verification_cooldown_wait_started",
                    message=f"第 {index + 1} 条视频已预检，正在等待短信验证冷却",
                )
            except Exception:
                raise DouyinCommerceBatchExecutorError(
                    "verification_cooldown_failed"
                ) from None

            def on_tick(remaining_seconds: int) -> None:
                self._emit(
                    progress,
                    index=index,
                    total=total,
                    phase="verification_cooldown",
                    message=f"第 {index + 1} 条视频距可提交还有 {remaining_seconds} 秒",
                    remaining_seconds=remaining_seconds,
                )

            ready = self._cooldown_gate.wait_until_ready(
                account_key,
                on_tick=on_tick,
                cancelled=cancelled,
            )
            if ready:
                try:
                    self._record_progress(
                        task_id,
                        item_id,
                        ok=True,
                        event_type="verification_cooldown_wait_finished",
                        message=f"第 {index + 1} 条视频短信验证冷却已结束",
                    )
                except Exception:
                    raise DouyinCommerceBatchExecutorError(
                        "verification_cooldown_failed"
                    ) from None
            return ready
        except DouyinSmsCooldownError as exc:
            raise DouyinCommerceBatchExecutorError(_text(exc)) from None

    def _run(
        self,
        batch: Mapping[str, Any],
        *,
        task_id: int,
        publish: bool,
        progress: Callable[[BatchProgressEvent], None] | None,
        run_generation: int,
    ) -> list[dict[str, object]]:
        items = list(batch["items"])
        task_items = self._task_item_ids(task_id, len(items))
        total = len(items)
        results: list[dict[str, object]] = []
        paused = False
        paused_result_status = "pending"
        consecutive_failures = 0
        pause_reason = ""

        for index, (item, item_id) in enumerate(zip(items, task_items)):
            label = _safe_item_label(item, index)
            if paused:
                results.append(
                    {"index": index, "label": label, "status": paused_result_status}
                )
                continue
            if publish and self._run_was_cancelled(run_generation):
                pause_reason = "客户端已退出，当前视频未执行最终提交"
                self._task_store.pause_douyin_batch_before_submit(
                    task_id,
                    item_id,
                    pause_reason,
                )
                results.append(
                    {"index": index, "label": label, "status": "client_shutdown"}
                )
                paused = True
                paused_result_status = "pending"
                continue
            if self._pause_requested.is_set():
                paused = True
                paused_result_status = "paused"
                pause_reason = "已按用户请求暂停，未开始后续视频"
                self._task_store.mark_task_paused(
                    task_id,
                    pause_reason,
                    pause_reason_code=task_service.PAUSE_REASON_USER_REQUEST,
                )
                self._emit(
                    progress,
                    index=index,
                    total=total,
                    phase="paused",
                    message=pause_reason,
                )
                results.append({"index": index, "label": label, "status": "paused"})
                continue
            result = self._run_item(
                batch,
                item,
                task_id=task_id,
                item_id=item_id,
                index=index,
                total=total,
                publish=publish,
                progress=progress,
                run_generation=run_generation,
            )
            results.append(result)
            if result["status"] == "client_shutdown":
                paused = True
                paused_result_status = "pending"
                continue
            if result["status"] in {
                "waiting_login",
                "waiting_verification",
                "verification_failed",
            }:
                paused = True
                paused_result_status = "pending"
                pause_reason = "等待用户处理验证或登录，未开始后续视频"
                pause_reason_code = (
                    task_service.PAUSE_REASON_WAITING_LOGIN
                    if result["status"] == "waiting_login"
                    else task_service.PAUSE_REASON_WAITING_VERIFICATION
                )
                self._task_store.mark_task_paused(
                    task_id,
                    pause_reason,
                    pause_reason_code=pause_reason_code,
                )
                continue
            if result["status"] == "receipt_ambiguous":
                paused = True
                paused_result_status = "pending"
                pause_reason = "平台最终提交状态待核对，已暂停且未开始后续视频"
                self._task_store.mark_task_paused(
                    task_id,
                    pause_reason,
                    pause_reason_code=task_service.PAUSE_REASON_RECEIPT_AMBIGUOUS,
                )
                continue
            if result["status"] == "failed":
                consecutive_failures += 1
            else:
                consecutive_failures = 0
            if consecutive_failures >= 5:
                paused = True
                paused_result_status = "paused"
                pause_reason = "连续失败 5 条，已自动暂停，未开始后续视频"
                self._task_store.mark_task_paused(
                    task_id,
                    pause_reason,
                    pause_reason_code=task_service.PAUSE_REASON_AUTO_FAILURE,
                )
                self._emit(
                    progress,
                    index=index,
                    total=total,
                    phase="auto_paused",
                    message=pause_reason,
                )
        return results

    def _run_item(
        self,
        batch: Mapping[str, Any],
        item: Mapping[str, Any],
        *,
        task_id: int,
        item_id: int,
        index: int,
        total: int,
        publish: bool,
        progress: Callable[[BatchProgressEvent], None] | None,
        run_generation: int,
    ) -> dict[str, object]:
        label = _safe_item_label(item, index)
        session_id = ""
        retain_session = False
        submit_permission_claimed = False
        final_receipt_recorded = False
        # 上传、地点和预检都必须留在 dry-run 会话；上传阶段已完成标题、文案
        # 与话题的页面回读，不能在同一编辑页重复同步一次内容。重复清空富文本
        # 编辑器会在 Windows 端偶发残留旧文案，且没有任何业务收益。
        # 只有通过同一会话的最终 submit 才可显式切换为 publish。
        payload = _runtime_payload(batch, item, mode="preflight")
        publish_payload = dict(payload)
        publish_payload["runtimeMode"] = "publish"
        publish_payload["debugDryRun"] = False
        try:
            self._emit(progress, index=index, total=total, phase="uploading", message=f"正在上传第 {index + 1} 条视频")
            upload = self._manager.start_upload(payload)
            login_pause = _controlled_upload_pause_status(upload)
            if login_pause:
                waiting = self._record_login_waiting(
                    task_id,
                    item_id,
                    index=index,
                    total=total,
                    progress=progress,
                )
                return {**waiting, "label": label}
            if not isinstance(upload, Mapping) or not _text(upload.get("sessionId")):
                raise DouyinCommerceBatchExecutorError("抖音上传会话未返回唯一会话标识")
            session_id = _text(upload.get("sessionId"))

            baseline = self._manager.prepare_publish_settings(session_id)
            if (
                not isinstance(baseline, Mapping)
                or baseline.get("status") != "clean"
                or type(baseline.get("openLayerCount")) is not int
                or baseline.get("openLayerCount") != 0
            ):
                raise DouyinCommerceBatchExecutorError(
                    "抖音正式发布页未取得干净设置基线"
                )

            selected = self._manager.select_cached_favorite_music(
                session_id, _text(payload["selectedMusic"].get("musicId"))
            )
            if not isinstance(selected, Mapping) or _text(selected.get("musicId")) != _text(payload["selectedMusic"].get("musicId")):
                raise DouyinCommerceBatchExecutorError("抖音收藏音乐未能按已选音乐回读")
            declared = self._manager.select_content_declaration(
                session_id, _text(payload["contentDeclaration"])
            )
            if _text(declared) != _text(payload["contentDeclaration"]):
                raise DouyinCommerceBatchExecutorError("抖音作品内容声明回读不一致")

            location = payload["locationPoi"]
            scope = _text(payload["locationScope"])
            location_keywords = _location_search_keywords(
                location,
                original_keyword=payload.get("locationSearchKeyword"),
            )
            scope_label = {"local": "本地", "domestic": "国内"}.get(
                scope,
                scope or "未确认",
            )
            target_location = (
                f"{_text(location.get('name'))} · {_text(location.get('address'))}"
            ).strip(" ·")
            douyin_logger.info(
                f"抖音带货第 {index + 1}/{total} 条开始恢复发布定位："
                f"视频={label}，范围={scope_label}，目标={target_location}，"
                f"原始搜索词={_text(payload.get('locationSearchKeyword')) or '旧任务未保存'}，"
                f"依次尝试={location_keywords}"
            )
            try:
                applied = self._manager.apply_saved_location(
                    session_id,
                    location,
                    scope,
                    location_keywords,
                )
            except Exception as exc:
                douyin_logger.warning(
                    f"抖音带货第 {index + 1}/{total} 条发布定位恢复失败："
                    f"视频={label}，范围={scope_label}，目标={target_location}，"
                    f"已尝试={location_keywords}，错误={_text(exc) or type(exc).__name__}"
                )
                raise
            applied_location = (
                applied.get("location")
                if isinstance(applied, Mapping) and isinstance(applied.get("location"), Mapping)
                else None
            )
            try:
                confirmed_location = match_location_preset(
                    location,
                    [dict(applied_location)]
                    if isinstance(applied_location, Mapping)
                    else [],
                )
            except Exception:
                raise DouyinCommerceBatchExecutorError(
                    "publish_location_readback_mismatch"
                ) from None
            if not isinstance(applied_location, Mapping) or any(
                _text(applied_location.get(field))
                != _text(confirmed_location.get(field))
                for field in ("poiId", "name", "address")
            ):
                raise DouyinCommerceBatchExecutorError(
                    "publish_location_readback_mismatch"
                )
            douyin_logger.success(
                f"抖音带货第 {index + 1}/{total} 条发布定位已恢复并回读确认："
                f"{target_location}"
            )

            self._manager.sync_schedule(session_id, payload)
            preflight = self._manager.preflight(session_id, payload)
            self._record_progress(
                task_id, item_id, ok=True, event_type="preflight_readback",
                message=f"第 {index + 1} 条视频已完成编辑页预检回读",
            )
            if not publish:
                self._emit(progress, index=index, total=total, phase="preflighted", message=f"第 {index + 1} 条视频预检完成")
                return {"index": index, "label": label, "status": "preflighted", "readback": dict(preflight or {})}

            if not self._wait_for_sms_cooldown(
                _text(batch.get("accountFile")),
                task_id=task_id,
                item_id=item_id,
                index=index,
                total=total,
                progress=progress,
                run_generation=run_generation,
            ):
                message = "客户端已退出，当前视频未执行最终提交"
                self._task_store.pause_douyin_batch_before_submit(
                    task_id,
                    item_id,
                    message,
                )
                return {
                    "index": index,
                    "label": label,
                    "status": "client_shutdown",
                }

            self._emit(progress, index=index, total=total, phase="submitting", message=f"正在提交第 {index + 1} 条视频")
            verification_error_sentinel: list[str] = []
            verification_callback = self._verification_progress_callback(
                task_id=task_id,
                item_id=item_id,
                index=index,
                total=total,
                label=label,
                account_key=_text(batch.get("accountFile")),
                run_generation=run_generation,
                progress=progress,
                error_sentinel=verification_error_sentinel,
            )
            submit_error: Exception | None = None
            submit_key = (run_generation, int(task_id), int(item_id))
            submitted = None
            with self._run_condition:
                if not (
                    self._shutdown_requested.is_set()
                    or run_generation != self._run_generation
                    or submit_key in self._claimed_submit_items
                ):
                    self._claimed_submit_items.add(submit_key)
                    self._active_submit_leases.add(submit_key)
                    submit_permission_claimed = True
            if submit_permission_claimed:
                try:
                    try:
                        submitted = self._manager.submit(
                            session_id,
                            publish_payload,
                            task_id=task_id,
                            on_verification=verification_callback,
                        )
                    except (KeyboardInterrupt, SystemExit):
                        raise
                    except Exception as exc:
                        submit_error = exc
                        submitted = None
                finally:
                    with self._run_condition:
                        self._active_submit_leases.discard(submit_key)
                        self._run_condition.notify_all()
            if not submit_permission_claimed:
                message = "客户端已退出，当前视频未执行最终提交"
                self._task_store.pause_douyin_batch_before_submit(
                    task_id,
                    item_id,
                    message,
                )
                return {
                    "index": index,
                    "label": label,
                    "status": "client_shutdown",
                }
            if verification_error_sentinel:
                diagnostic = verification_error_sentinel[0]
                message = (
                    f"第 {index + 1} 条视频已进入最终提交，"
                    f"但冷却状态异常：{diagnostic}"
                )
                self._record_progress(
                    task_id,
                    item_id,
                    ok=False,
                    event_type="platform_receipt_ambiguous",
                    message=message,
                )
                self._emit(
                    progress,
                    index=index,
                    total=total,
                    phase="receipt_ambiguous",
                    message=message,
                )
                return {
                    "index": index,
                    "label": label,
                    "status": "receipt_ambiguous",
                    "diagnostic": diagnostic,
                }
            if submit_error is not None:
                raise submit_error
            self._record_final_submit_result(
                task_id,
                item_id,
                submitted,
                expected_schedule_time=_text(publish_payload.get("scheduleTime")),
            )
            final_receipt_recorded = True
            self._emit(progress, index=index, total=total, phase="published", message=f"第 {index + 1} 条视频已取得平台回读")
            return {"index": index, "label": label, "status": "published"}
        except (KeyboardInterrupt, SystemExit):
            if not submit_permission_claimed:
                self._task_store.pause_douyin_batch_before_submit(
                    task_id,
                    item_id,
                    "客户端已退出，当前视频未执行最终提交",
                )
            elif not final_receipt_recorded:
                message = "最终提交已开始，但平台回执状态待人工核对"
                self._record_progress(
                    task_id,
                    item_id,
                    ok=False,
                    event_type="platform_receipt_ambiguous",
                    message=message,
                )
                self._task_store.mark_task_paused(
                    task_id,
                    message,
                    pause_reason_code=task_service.PAUSE_REASON_RECEIPT_AMBIGUOUS,
                )
            raise
        except Exception as exc:
            diagnostic = _text(exc)[:240] or "未取得可用的平台回执"
            if self._has_active_verification(task_id):
                # 仅 broker 仍持有同一 active 请求时才保留会话。真实 manager 在
                # 该会话内等待用户输入并从当前 submit 调用继续，因此不会重复提交。
                retain_session = True
                waiting = self._record_active_verification_waiting(
                    task_id,
                    item_id,
                    index=index,
                    total=total,
                    progress=progress,
                )
                return {**waiting, "label": label, "diagnostic": diagnostic}
            if _is_ambiguous_submit_error(exc):
                message = (
                    f"第 {index + 1} 条视频已触发最终提交，但平台状态待核对："
                    f"{diagnostic}"
                )
                self._record_progress(
                    task_id,
                    item_id,
                    ok=False,
                    event_type="platform_receipt_ambiguous",
                    message=message,
                )
                self._emit(
                    progress,
                    index=index,
                    total=total,
                    phase="receipt_ambiguous",
                    message=message,
                )
                return {
                    "index": index,
                    "label": label,
                    "status": "receipt_ambiguous",
                    "diagnostic": diagnostic,
                }
            if _is_intervention_error(exc):
                verification_message = f"第 {index + 1} 条视频：{diagnostic}"
                self._record_progress(
                    task_id,
                    item_id,
                    ok=False,
                    event_type="verification_failed",
                    message=verification_message,
                )
                self._emit(
                    progress,
                    index=index,
                    total=total,
                    phase="verification_failed",
                    message=verification_message,
                )
                return {
                    "index": index,
                    "label": label,
                    "status": "verification_failed",
                    "diagnostic": diagnostic,
                }
            self._record_progress(
                task_id, item_id, ok=False, event_type="batch_item_failed",
                message=(
                    f"第 {index + 1} 条视频未完成平台回读："
                    f"{_public_batch_diagnostic(diagnostic)}"
                ),
            )
            public_diagnostic = _public_batch_diagnostic(diagnostic)
            self._emit(
                progress,
                index=index,
                total=total,
                phase="failed",
                message=f"第 {index + 1} 条视频失败：{public_diagnostic}；继续下一条",
            )
            return {
                "index": index,
                "label": label,
                "status": "failed",
                "diagnostic": public_diagnostic,
            }
        finally:
            if session_id and not retain_session:
                self._manager.close(session_id)


# 发布服务只通过这个受控实例进入批量执行器；实际成功回执仍只能由同一 submit
# 会话的内部写入器落库，模块实例本身不暴露任何“字典写成功”入口。
batch_executor = DouyinCommerceBatchExecutor()
