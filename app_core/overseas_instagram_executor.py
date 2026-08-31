# -*- coding: utf-8 -*-
"""Instagram 同一表单会话的受控执行状态机。

真实浏览器适配器尚未在本阶段开放。这里只定义平台无关的调度边界，
便于 UI 和 CLI 后续共用一个 worker，并用离线假适配器验证不可逆门。
"""

from __future__ import annotations

from typing import Mapping, Protocol, Sequence

from . import overseas_instagram_claims as claims
from .overseas_instagram_errors import (
    InstagramPublishError,
    project_instagram_receipt,
)
from .overseas_instagram_identity import (
    InstagramIdentity,
    InstagramIdentityError,
    confirm_two_page_identity,
    parse_instagram_identity_payload,
)
from .overseas_instagram_publish import (
    InstagramContentBaseline,
    InstagramPublishIntent,
    capture_instagram_content_baseline,
    match_unique_instagram_readback,
    prepare_instagram_publish_intent,
    verify_instagram_form_snapshot,
)


class InstagramPageSession(Protocol):
    """真实适配器必须在同一受控浏览器会话实现的最小合同。"""

    def read_identity(self, surface: str) -> Mapping[str, object]: ...

    def read_content_baseline(
        self, intent: InstagramPublishIntent
    ) -> Mapping[str, object]: ...

    def fill_and_read_form(
        self,
        intent: InstagramPublishIntent,
        identity: InstagramIdentity,
    ) -> Mapping[str, object]: ...

    def read_current_form(
        self,
        intent: InstagramPublishIntent,
        identity: InstagramIdentity,
    ) -> Mapping[str, object]: ...

    def click_final_action_once(
        self, intent: InstagramPublishIntent
    ) -> Mapping[str, object]: ...

    def read_content_after_final(
        self, intent: InstagramPublishIntent
    ) -> Sequence[Mapping[str, object]]: ...


def _identity_error(exc: InstagramIdentityError) -> InstagramPublishError:
    return InstagramPublishError(exc.error_code, exc.public_message)


def _safe_error_text(exc: BaseException) -> str:
    value = " ".join(str(exc).split())
    folded = value.casefold()
    if (
        not value
        or len(value) > 512
        or any(
            marker in folded
            for marker in ("cookie=", "access_token", "authorization:")
        )
    ):
        return f"{type(exc).__name__}: Instagram platform result unavailable"
    return value


class InstagramControlledExecutor:
    """先停在 Share 前，获得任务级授权后才消费唯一点击权。"""

    def __init__(
        self,
        payload: Mapping[str, object],
        *,
        saved_identity: InstagramIdentity,
        session: InstagramPageSession,
    ) -> None:
        self.intent = prepare_instagram_publish_intent(payload)
        if not isinstance(saved_identity, InstagramIdentity):
            raise InstagramPublishError(
                "instagram_account_invalid",
                "Instagram 本地账号缺少稳定身份绑定。",
            )
        self.saved_identity = saved_identity
        self.session = session
        self.identity: InstagramIdentity | None = None
        self.baseline: InstagramContentBaseline | None = None
        self.preflight_receipt: dict[str, object] | None = None

    def run_platform_form_preflight(self) -> dict[str, object]:
        """填写并精确回读，保留当前会话，不点击最终按钮。"""

        if self.preflight_receipt is not None:
            raise InstagramPublishError(
                "instagram_preflight_already_completed",
                "Instagram 当前表单会话已完成预检。",
            )
        try:
            composer = parse_instagram_identity_payload(
                self.session.read_identity("composer")
            )
            content = parse_instagram_identity_payload(
                self.session.read_identity("content")
            )
            observed = confirm_two_page_identity(composer, content)
            confirm_two_page_identity(self.saved_identity, observed)
        except InstagramIdentityError as exc:
            raise _identity_error(exc) from exc
        if observed.user_id != self.intent.instagram_user_id:
            raise InstagramPublishError(
                "instagram_identity_mismatch",
                "Instagram 表单主体与任务中的绑定账号不一致。",
            )
        raw_baseline = self.session.read_content_baseline(self.intent)
        if not isinstance(raw_baseline, Mapping):
            raise InstagramPublishError(
                "instagram_baseline_unavailable",
                "Instagram 内容列表基线不可用。",
            )
        baseline = capture_instagram_content_baseline(
            instagram_user_id=self.intent.instagram_user_id,
            content_ids=raw_baseline.get("contentIds"),
            complete=raw_baseline.get("complete") is True,
            captured_at=str(raw_baseline.get("capturedAt") or ""),
        )
        raw_evidence = self.session.fill_and_read_form(self.intent, observed)
        if not isinstance(raw_evidence, Mapping):
            raise InstagramPublishError(
                "instagram_form_readback_failed",
                "Instagram 表单回读结构无效，已停在分享之前。",
            )
        supplied_baseline = raw_evidence.get("baselineHash")
        if supplied_baseline not in (None, baseline.baseline_hash):
            raise InstagramPublishError(
                "instagram_baseline_unavailable",
                "Instagram 表单会话的内容基线已变化。",
            )
        receipt = verify_instagram_form_snapshot(
            self.intent,
            observed,
            {**dict(raw_evidence), "baselineHash": baseline.baseline_hash},
        )
        self.identity = observed
        self.baseline = baseline
        self.preflight_receipt = project_instagram_receipt(receipt)
        return dict(self.preflight_receipt)

    def _mark_unknown(
        self,
        conn,
        *,
        task_id: int,
        exc: BaseException,
        observed_at: str,
    ) -> InstagramPublishError:
        error_code = str(
            getattr(exc, "error_code", "instagram_publish_outcome_unknown")
            or "instagram_publish_outcome_unknown"
        )
        if not error_code.startswith("instagram_"):
            error_code = "instagram_publish_outcome_unknown"
        platform_text = _safe_error_text(exc)
        claims.mark_instagram_outcome_unknown(
            conn,
            task_id=task_id,
            error_code=error_code,
            platform_error_text=platform_text,
            observed_at=observed_at,
        )
        conn.commit()
        claim = claims.get_instagram_claim(conn, task_id)
        return InstagramPublishError(
            error_code,
            platform_text,
            receipt={
                "phase": "outcome_unknown",
                "accountId": self.intent.account_id,
                "instagramUserId": self.intent.instagram_user_id,
                "mediaId": None,
                "url": None,
                "finalActionTriggered": bool(
                    claim.get("finalActionTriggered") == 1
                ),
                "blocksReplay": True,
                "errorCode": error_code,
                "platformErrorText": platform_text,
            },
            outcome_ambiguous=True,
        )

    def run_authorized_final_action(
        self,
        conn,
        *,
        task_id: int,
        claimed_at: str,
    ) -> dict[str, object]:
        """消费已预留 Claim 的唯一点击权，然后只接受唯一回读。"""

        if self.preflight_receipt is None or self.baseline is None:
            raise InstagramPublishError(
                "instagram_platform_preflight_required",
                "Instagram 正式动作必须续接当前已验证表单会话。",
            )
        if self.identity is None:
            raise InstagramPublishError(
                "instagram_platform_preflight_required",
                "Instagram 当前表单会话缺少已回读主体。",
            )
        snapshot_hash = self.preflight_receipt.get("formSnapshotHash")
        try:
            current_evidence = self.session.read_current_form(
                self.intent,
                self.identity,
            )
            if not isinstance(current_evidence, Mapping):
                raise InstagramPublishError(
                    "instagram_form_readback_failed",
                    "Instagram 最终动作前的表单回读结构无效。",
                )
            current_receipt = verify_instagram_form_snapshot(
                self.intent,
                self.identity,
                {
                    **dict(current_evidence),
                    "baselineHash": self.baseline.baseline_hash,
                },
            )
            if current_receipt.get("formSnapshotHash") != snapshot_hash:
                raise InstagramPublishError(
                    "instagram_preflight_stale",
                    "Instagram 当前表单与已授权预检快照不一致。",
                )
        except Exception as exc:
            error_code = str(
                getattr(exc, "error_code", "instagram_form_readback_failed")
                or "instagram_form_readback_failed"
            )
            if not error_code.startswith("instagram_"):
                error_code = "instagram_form_readback_failed"
            claims.mark_instagram_safe_failed(
                conn,
                task_id=task_id,
                error_code=error_code,
                platform_error_text=_safe_error_text(exc),
                observed_at=claimed_at,
            )
            conn.commit()
            if isinstance(exc, InstagramPublishError):
                raise exc
            raise InstagramPublishError(
                error_code,
                _safe_error_text(exc),
            ) from exc
        claims.claim_instagram_final_action(
            conn,
            task_id=task_id,
            form_snapshot_hash=str(snapshot_hash or ""),
            claimed_at=claimed_at,
        )
        # 点击前必须先提交 Claim；即使进程随后崩溃，也不能重放。
        conn.commit()
        click_observed_at = claimed_at
        try:
            click_result = self.session.click_final_action_once(self.intent)
            if not isinstance(click_result, Mapping):
                raise InstagramPublishError(
                    "instagram_publish_outcome_unknown",
                    "Instagram 最终动作没有返回可核对结果。",
                    outcome_ambiguous=True,
                )
            click_observed_at = str(
                click_result.get("observedAt") or claimed_at
            )
            claims.record_instagram_final_action_clicked(
                conn,
                task_id=task_id,
                clicked_at=click_observed_at,
            )
            conn.commit()
            if click_result.get("accepted") is not True:
                raise InstagramPublishError(
                    "instagram_publish_outcome_unknown",
                    str(
                        click_result.get("platformErrorText")
                        or "Instagram 没有返回明确受理结果。"
                    ),
                    outcome_ambiguous=True,
                )
            claims.mark_instagram_platform_accepted(
                conn,
                task_id=task_id,
                observed_at=click_observed_at,
            )
            conn.commit()
            receipt = match_unique_instagram_readback(
                self.baseline,
                self.intent,
                self.session.read_content_after_final(self.intent),
            )
            observed_at = str(
                receipt.get("publishedAt")
                or receipt.get("scheduledAt")
                or click_observed_at
            )
            claims.mark_instagram_readback_success(
                conn,
                task_id=task_id,
                receipt=receipt,
                observed_at=observed_at,
            )
            conn.commit()
            return project_instagram_receipt(receipt)
        except Exception as exc:
            try:
                current = claims.get_instagram_claim(conn, task_id)
                if str(current.get("state") or "") not in {
                    "ambiguous",
                    "succeeded",
                }:
                    raise self._mark_unknown(
                        conn,
                        task_id=task_id,
                        exc=exc,
                        observed_at=click_observed_at,
                    )
            except InstagramPublishError as marked:
                if marked.outcome_ambiguous:
                    raise marked from exc
                raise
            if isinstance(exc, InstagramPublishError):
                raise exc
            raise InstagramPublishError(
                "instagram_publish_outcome_unknown",
                _safe_error_text(exc),
                outcome_ambiguous=True,
            ) from exc
