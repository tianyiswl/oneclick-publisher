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


def _final_receipt_from_submit_result(result: object) -> tuple[str, dict[str, str], str]:
    """只接受会话 submit 返回的最终管理页回读，不补造日期或时区。"""

    if not isinstance(result, Mapping) or result.get("ok") is not True:
        raise DouyinCommerceBatchExecutorError("抖音批量执行器缺少成功的平台最终结果")
    message = _text(result.get("message"))
    if not message:
        raise DouyinCommerceBatchExecutorError("抖音批量执行器缺少平台最终回读说明")

    if result.get("scheduled") is True:
        readback = result.get("scheduledReadback")
        if not isinstance(readback, Mapping):
            raise DouyinCommerceBatchExecutorError("抖音定时提交缺少平台管理页回读")
        receipt = {
            "scheduleTime": _text(readback.get("scheduledAt")),
            "timezone": _text(readback.get("timezone")),
        }
        return "platform_scheduled_receipt", receipt, message

    readback = result.get("platformReceipt")
    if not isinstance(readback, Mapping):
        raise DouyinCommerceBatchExecutorError("抖音发表缺少平台最终回读")
    receipt = {
        key: _text(readback.get(key))
        for key in ("platformPostId", "postUrl", "publishedAt", "timezone")
        if _text(readback.get(key))
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

    def run_preflight(
        self,
        batch: Mapping[str, Any],
        *,
        task_id: int,
        progress: Callable[[BatchProgressEvent], None] | None = None,
    ) -> list[dict[str, object]]:
        """逐条写入并回读编辑器字段；绝不调用 submit。"""

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
    ) -> None:
        """执行器唯一的成功写入点：只消费本次 submit 最终回读。"""

        event_type, receipt, message = _final_receipt_from_submit_result(result)
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

        for index, (item, item_id) in enumerate(zip(items, task_items)):
            label = _safe_item_label(item, index)
            if paused:
                results.append({"index": index, "label": label, "status": "pending"})
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
        # 上传、内容写入、地点和预检都必须留在 dry-run 会话；只有通过同一
        # 会话的最终 submit 才可显式切换为 publish。
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

            self._manager.synchronize_content(session_id, payload)
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

            candidates = self._manager.search_locations(
                session_id,
                _text(payload["locationKeyword"]),
                _text(payload["locationScope"]),
            )
            try:
                matched = match_location_preset(payload["locationPoi"], candidates)
            except DouyinLocationPresetError as exc:
                raise DouyinCommerceBatchExecutorError(str(exc)) from exc
            applied = self._manager.apply_location(session_id, matched)
            if not isinstance(applied, Mapping) or any(
                _text(applied.get(field)) != _text(matched.get(field))
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
            self._record_final_submit_result(task_id, item_id, submitted)
            self._emit(progress, index=index, total=total, phase="published", message=f"第 {index + 1} 条视频已取得平台回读")
            return {"index": index, "label": label, "status": "published"}
        except Exception as exc:
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
                return {**waiting, "label": label}
            if _is_intervention_error(exc):
                self._record_progress(
                    task_id,
                    item_id,
                    ok=False,
                    event_type="verification_failed",
                    message=f"第 {index + 1} 条视频的抖音验证未完成，批量已安全停止",
                )
                self._emit(
                    progress,
                    index=index,
                    total=total,
                    phase="verification_failed",
                    message=f"第 {index + 1} 条视频验证失败或已取消，批量已停止",
                )
                return {"index": index, "label": label, "status": "verification_failed"}
            self._record_progress(
                task_id, item_id, ok=False, event_type="batch_item_failed",
                message=f"第 {index + 1} 条视频未完成平台回读，已跳过继续下一条",
            )
            self._emit(progress, index=index, total=total, phase="failed", message=f"第 {index + 1} 条视频未完成，继续下一条")
            return {"index": index, "label": label, "status": "failed"}
        finally:
            if session_id and not retain_session:
                self._manager.close(session_id)
