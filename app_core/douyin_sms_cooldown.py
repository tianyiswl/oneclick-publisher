import math
import threading
import time


SMS_COOLDOWN_SECONDS = 60.0


class DouyinSmsCooldownError(RuntimeError):
    pass


class DouyinSmsCooldownGate:
    def __init__(self, *, clock=time.monotonic, waiter=time.sleep) -> None:
        self._clock = clock
        self._waiter = waiter
        self._lock = threading.RLock()
        self._deadlines: dict[str, float] = {}

    @staticmethod
    def _validated_key(value: object) -> str:
        if type(value) is not str or not value.strip():
            raise DouyinSmsCooldownError("verification_cooldown_state_invalid")
        return value.strip()

    def _safe_now(self) -> float:
        try:
            value = float(self._clock())
        except (TypeError, ValueError, OverflowError):
            raise DouyinSmsCooldownError(
                "verification_cooldown_state_invalid"
            ) from None
        if not math.isfinite(value):
            raise DouyinSmsCooldownError("verification_cooldown_state_invalid")
        return value

    @staticmethod
    def _validated_remaining(value: object) -> float:
        try:
            remaining = float(value)
        except (TypeError, ValueError, OverflowError):
            raise DouyinSmsCooldownError(
                "verification_cooldown_state_invalid"
            ) from None
        if not math.isfinite(remaining) or not 0 <= remaining <= 60:
            raise DouyinSmsCooldownError("verification_cooldown_state_invalid")
        return remaining

    def record_trigger(self, account_key: str) -> None:
        key = self._validated_key(account_key)
        with self._lock:
            deadline = self._safe_now() + SMS_COOLDOWN_SECONDS
            self._deadlines[key] = max(self._deadlines.get(key, deadline), deadline)

    def restore_remaining(self, account_key: str, remaining_seconds: float) -> None:
        key = self._validated_key(account_key)
        remaining = self._validated_remaining(remaining_seconds)
        restored = self._safe_now() + remaining
        with self._lock:
            self._deadlines[key] = max(self._deadlines.get(key, restored), restored)

    def remaining_seconds(self, account_key: str) -> int:
        return math.ceil(self._remaining(account_key))

    def _remaining(self, account_key: str) -> float:
        key = self._validated_key(account_key)
        now = self._safe_now()
        with self._lock:
            deadline = self._deadlines.get(key, now)
        return max(0.0, deadline - now)

    def wait_until_ready(self, account_key: str, *, on_tick, cancelled) -> bool:
        try:
            while True:
                remaining = self._remaining(account_key)
                if remaining <= 0:
                    return True
                if cancelled():
                    return False
                on_tick(math.ceil(remaining))
                if cancelled():
                    return False
                self._waiter(min(1.0, remaining))
        except DouyinSmsCooldownError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            raise DouyinSmsCooldownError("verification_cooldown_failed") from None
