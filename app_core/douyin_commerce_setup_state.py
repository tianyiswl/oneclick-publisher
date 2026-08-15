"""抖音平台设置采集器的代际状态与脱敏诊断契约。"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import re
from typing import Dict, Optional
from uuid import uuid4


class SetupGenerationState(str, Enum):
    NEW = "new"
    COLLECTING = "collecting"
    READY = "ready"
    CLOSING_COLLECTORS = "closing_collectors"
    PUBLISHING = "publishing"
    CANCELLING = "cancelling"
    PAUSED_FOR_LOGIN = "paused_for_login"
    CLOSED = "closed"


class CollectorType(str, Enum):
    DOMESTIC_LOCATION = "domestic_location"
    FAVORITE_MUSIC = "favorite_music"
    LOCAL_LOCATION = "local_location"


class CollectorState(str, Enum):
    NOT_STARTED = "not_started"
    STARTING = "starting"
    ACTIVE = "active"
    RETRYING = "retrying"
    FAILED = "failed"
    CLOSING = "closing"
    CLOSED = "closed"


class SetupGenerationStateError(ValueError):
    """代际状态转换不符合状态机定义。"""


_ALLOWED_TRANSITIONS = {
    SetupGenerationState.NEW: {
        SetupGenerationState.COLLECTING,
        SetupGenerationState.CANCELLING,
    },
    SetupGenerationState.COLLECTING: {
        SetupGenerationState.READY,
        SetupGenerationState.CLOSING_COLLECTORS,
        SetupGenerationState.CANCELLING,
        SetupGenerationState.PAUSED_FOR_LOGIN,
    },
    SetupGenerationState.READY: {
        SetupGenerationState.COLLECTING,
        SetupGenerationState.CLOSING_COLLECTORS,
        SetupGenerationState.CANCELLING,
        SetupGenerationState.PAUSED_FOR_LOGIN,
    },
    SetupGenerationState.CLOSING_COLLECTORS: {
        SetupGenerationState.PUBLISHING,
        SetupGenerationState.CLOSED,
    },
    SetupGenerationState.PUBLISHING: {SetupGenerationState.CLOSED},
    SetupGenerationState.CANCELLING: {SetupGenerationState.CLOSED},
    SetupGenerationState.PAUSED_FOR_LOGIN: {SetupGenerationState.CLOSED},
    SetupGenerationState.CLOSED: set(),
}

COLLECTOR_ERROR_CODES = frozenset(
    {
        "collector_start_failed",
        "login_required",
        "scope_not_confirmed",
        "candidate_panel_missing",
        "candidate_ambiguous",
        "candidate_empty",
        "rate_limited_or_degraded",
        "stale_result_discarded",
        "cleanup_incomplete",
        "publish_apply_mismatch",
        "collector_search_context_mismatch",
        "publish_location_load_more_failed",
        "collector_unknown",
    }
)

_ACCOUNT_MASKED_ID_PATTERN = re.compile(r"account-\d+\Z")


@dataclass
class CollectorSlot:
    collector_type: CollectorType
    state: CollectorState = CollectorState.NOT_STARTED
    instance_id: Optional[str] = None
    session_id: Optional[str] = None


@dataclass
class SetupGeneration:
    generation_id: str
    account_id: int
    state: SetupGenerationState = SetupGenerationState.NEW
    collectors: Dict[CollectorType, CollectorSlot] = field(default_factory=dict)

    def transition(self, next_state: SetupGenerationState) -> None:
        if next_state not in _ALLOWED_TRANSITIONS[self.state]:
            raise SetupGenerationStateError(
                "不允许从 {} 转换到 {}".format(self.state.value, next_state.value)
            )
        self.state = next_state

    def activate_collector(
        self,
        collector_type: CollectorType,
        *,
        instance_id: str,
        session_id: str,
    ) -> None:
        slot = self.collectors[collector_type]
        slot.instance_id = instance_id
        slot.session_id = session_id
        slot.state = CollectorState.ACTIVE

    def accepts_result(
        self,
        generation_id: str,
        collector_type: CollectorType,
        instance_id: str,
    ) -> bool:
        if self.state not in {
            SetupGenerationState.COLLECTING,
            SetupGenerationState.READY,
        }:
            return False
        slot = self.collectors[collector_type]
        return (
            generation_id == self.generation_id
            and slot.instance_id == instance_id
            and slot.state is CollectorState.ACTIVE
        )

    def close(self) -> None:
        for slot in self.collectors.values():
            slot.state = CollectorState.CLOSED
            slot.session_id = None
        self.state = SetupGenerationState.CLOSED


def new_setup_generation(account_id: int) -> SetupGeneration:
    return SetupGeneration(
        generation_id=str(uuid4()),
        account_id=account_id,
        collectors={
            collector_type: CollectorSlot(collector_type=collector_type)
            for collector_type in CollectorType
        },
    )


@dataclass(frozen=True)
class CollectorDiagnosticEvent:
    request_id: str
    setup_generation_id: str
    collector_type: CollectorType
    collector_instance_id: str
    account_masked_id: str
    phase: str
    action: str
    scope: str
    keyword: str
    attempt: int
    candidate_count: int
    duration_ms: int
    outcome: str
    error_code: str
    cleanup_result: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_public_dict(self) -> Dict[str, object]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "requestId": self.request_id,
            "setupGenerationId": self.setup_generation_id,
            "collectorType": self.collector_type.value,
            "collectorInstanceId": self.collector_instance_id,
            "accountMaskedId": self._public_account_masked_id(),
            "phase": self.phase,
            "action": self.action,
            "scope": self.scope,
            "keyword": self.keyword[:80],
            "attempt": self.attempt,
            "candidateCount": self.candidate_count,
            "durationMs": self.duration_ms,
            "outcome": self.outcome,
            "errorCode": self._public_error_code(),
            "cleanupResult": self.cleanup_result,
        }

    def _public_account_masked_id(self) -> str:
        if _ACCOUNT_MASKED_ID_PATTERN.fullmatch(self.account_masked_id):
            return self.account_masked_id
        return "account-unknown"

    def _public_error_code(self) -> str:
        if self.error_code in COLLECTOR_ERROR_CODES:
            return self.error_code
        return "collector_unknown"
