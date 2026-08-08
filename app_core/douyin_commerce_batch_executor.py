# -*- coding: utf-8 -*-
"""抖音带货批量任务的串行、无头会话协调器。

本模块不提供任何可直接传入字典写成功的公开桥接。成功只会在执行器从同一
submit 会话取得最终平台回读后，经内部写入器落入任务记录；预检、编辑页
字段写入、验证码等待和本地排期都不能被写成平台成功。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
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
from .douyin_verification import (
    DouyinVerificationError,
    VerificationChallenge,
    verification_broker as _default_verification_broker,
)


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

    def to_public_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "total": self.total,
            "phase": self.phase,
            "message": self.message,
        }


def _text(value: object) -> str:
    return " ".join(str(value or "").replace("\u200b", " ").split())


def _safe_item_label(item: Mapping[str, Any], index: int) -> str:
    name = Path(_text(item.get("mediaPath"))).name
    return name or f"第 {index + 1} 条视频"


def _location_search_keywords(location: Mapping[str, Any]) -> list[str]:
    """返回地点恢复时的有序检索词：地址优先，国内排序漂移时补充城市和店名。"""

    address = _text(location.get("address"))
    name = _text(location.get("name"))
    result = [address] if address else []
    city_source = address
    if "自治区" in city_source:
        city_source = city_source.split("自治区", 1)[1]
    elif "省" in city_source:
        city_source = city_source.split("省", 1)[1]
    city_match = re.search(r"([\u4e00-\u9fff]{2,}市)", city_source)
    city = _text(city_match.group(1)[:-1]) if city_match else ""
    if city and name:
        result.append(f"{city} {name}")
    if name:
        result.append(name)
    return list(dict.fromkeys(keyword for keyword in result if keyword))


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
    ) -> None:
        self._manager = manager
        self._task_store = task_store
        self._now = now or (lambda: datetime.now(_SHANGHAI))
        self._verification_broker = verification_broker
        self._pause_requested = threading.Event()

    def request_pause(self) -> bool:
        """请求在当前视频安全收束后暂停，绝不在中途打断平台写入。"""

        if self._pause_requested.is_set():
            return False
        self._pause_requested.set()
        return True

    def _reset_pause_request(self) -> None:
        self._pause_requested.clear()

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
        self._reset_pause_request()
        return self._run(
            self._prepare_batch(batch),
            task_id=task_id,
            publish=True,
            progress=progress,
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
    ) -> None:
        if progress is None:
            return
        try:
            progress(BatchProgressEvent(index, total, phase, message))
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

    def _has_active_verification(self, task_id: int) -> bool:
        """仅信任仍由同一进程 broker 持有的 active 验证请求。

        验证被取消、过期或失败后，broker 不会再返回 active 请求。此时绝不能
        用异常文本把失败伪装成可恢复的 ``waiting_verification``。
        """

        try:
            request_id = self._verification_broker.request_for_task(int(task_id))
            if not request_id:
                return False
            snapshot = self._verification_broker.snapshot(request_id)
        except Exception:
            return False
        return _text(snapshot.get("state")) in _ACTIVE_VERIFICATION_STATES

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
        progress: Callable[[BatchProgressEvent], None] | None,
    ) -> Callable[[object], object]:
        """为原生验证弹窗附上内存条目上下文，不把挑战对象写入任务记录。"""

        def _callback(challenge: object) -> object:
            if isinstance(challenge, VerificationChallenge):
                # 这份副本只存在回调栈中；二维码字节仍由 broker 在内存托管。
                challenge = replace(challenge, item_index=index, item_label=label)
            # session manager 仅会在 broker 请求已经创建后调用回调。没有 active
            # 请求时不落 waiting 事件，以免已取消/过期的验证码留下可恢复假象。
            if self._has_active_verification(task_id):
                self._record_active_verification_waiting(
                    task_id,
                    item_id,
                    index=index,
                    total=total,
                    progress=progress,
                )
            return challenge

        return _callback

    def _run(
        self,
        batch: Mapping[str, Any],
        *,
        task_id: int,
        publish: bool,
        progress: Callable[[BatchProgressEvent], None] | None,
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
            if self._pause_requested.is_set():
                paused = True
                paused_result_status = "paused"
                pause_reason = "已按用户请求暂停，未开始后续视频"
                self._task_store.mark_task_paused(task_id, pause_reason)
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
            )
            results.append(result)
            if result["status"] in {
                "waiting_login",
                "waiting_verification",
                "verification_failed",
            }:
                paused = True
                paused_result_status = "pending"
                pause_reason = "等待用户处理验证或登录，未开始后续视频"
                continue
            if result["status"] == "receipt_ambiguous":
                paused = True
                paused_result_status = "pending"
                pause_reason = "平台最终提交状态待核对，已暂停且未开始后续视频"
                self._task_store.mark_task_paused(task_id, pause_reason)
                continue
            if result["status"] == "failed":
                consecutive_failures += 1
            else:
                consecutive_failures = 0
            if consecutive_failures >= 5:
                paused = True
                paused_result_status = "paused"
                pause_reason = "连续失败 5 条，已自动暂停，未开始后续视频"
                self._task_store.mark_task_paused(task_id, pause_reason)
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
    ) -> dict[str, object]:
        label = _safe_item_label(item, index)
        session_id = ""
        retain_session = False
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
            matched: dict[str, str] | None = None
            last_location_error: Exception | None = None
            for keyword in _location_search_keywords(location):
                try:
                    candidates = self._manager.search_locations(session_id, keyword, scope)
                    matched = match_location_preset(location, candidates)
                    break
                except Exception as exc:
                    last_location_error = exc
            if matched is None:
                diagnostic = _text(last_location_error) or "未返回可用候选"
                raise DouyinCommerceBatchExecutorError(
                    f"抖音在“{scope}”范围内未找到已保存的完整地点：{diagnostic}"
                )
            applied = self._manager.apply_location(session_id, matched)
            # 会话管理器的真实回读格式为 {"location": {...}}。离线替身可能
            # 直接返回地点对象，二者都只接受名称、完整地址与 POI 三项一致。
            applied_location = (
                applied.get("location")
                if isinstance(applied, Mapping) and isinstance(applied.get("location"), Mapping)
                else applied
            )
            if not isinstance(applied_location, Mapping) or any(
                _text(applied_location.get(field)) != _text(matched.get(field))
                for field in ("poiId", "name", "address")
            ):
                raise DouyinCommerceBatchExecutorError("抖音发布定位写入后回读不一致")

            self._manager.sync_schedule(session_id, payload)
            preflight = self._manager.preflight(session_id, payload)
            self._record_progress(
                task_id, item_id, ok=True, event_type="preflight_readback",
                message=f"第 {index + 1} 条视频已完成编辑页预检回读",
            )
            if not publish:
                self._emit(progress, index=index, total=total, phase="preflighted", message=f"第 {index + 1} 条视频预检完成")
                return {"index": index, "label": label, "status": "preflighted", "readback": dict(preflight or {})}

            self._emit(progress, index=index, total=total, phase="submitting", message=f"正在提交第 {index + 1} 条视频")
            submitted = self._manager.submit(
                session_id,
                publish_payload,
                task_id=task_id,
                on_verification=self._verification_progress_callback(
                    task_id=task_id,
                    item_id=item_id,
                    index=index,
                    total=total,
                    label=label,
                    progress=progress,
                ),
            )
            self._record_final_submit_result(
                task_id,
                item_id,
                submitted,
                expected_schedule_time=_text(publish_payload.get("scheduleTime")),
            )
            self._emit(progress, index=index, total=total, phase="published", message=f"第 {index + 1} 条视频已取得平台回读")
            return {"index": index, "label": label, "status": "published"}
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
                message=f"第 {index + 1} 条视频未完成平台回读：{diagnostic}",
            )
            self._emit(
                progress,
                index=index,
                total=total,
                phase="failed",
                message=f"第 {index + 1} 条视频失败：{diagnostic}；继续下一条",
            )
            return {
                "index": index,
                "label": label,
                "status": "failed",
                "diagnostic": diagnostic,
            }
        finally:
            if session_id and not retain_session:
                self._manager.close(session_id)


# 发布服务只通过这个受控实例进入批量执行器；实际成功回执仍只能由同一 submit
# 会话的内部写入器落库，模块实例本身不暴露任何“字典写成功”入口。
batch_executor = DouyinCommerceBatchExecutor()
