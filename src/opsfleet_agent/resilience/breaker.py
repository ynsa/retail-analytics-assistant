"""Circuit breaker: stop calls to a service after repeated failures (spec 36, B2.4).

    closed --3 failures in a row--> open --60 s later--> half_open --trial succeeds--> closed
                                                                   --trial fails-----> open

A failure is a temporary failure after all retries: a timeout, a 429 that is not the daily quota,
a 5xx or a connection error. A 400 or a SQL error is not a failure: the service answered.
A daily-quota error opens the breaker until the next reset of the quota (`record_failure(until=…)`).
The state lives in memory, for one process (one CLI user).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

BreakerState = Literal["closed", "open", "half_open"]


def utc_now() -> datetime:
    return datetime.now(UTC)


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        threshold: int = 3,
        open_seconds: float = 60.0,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.name = name
        self.threshold = threshold
        self.open_seconds = open_seconds
        self._clock = clock
        self.failures = 0  # failures in a row
        self.open_until: datetime | None = None
        self.reason: str = ""  # "failures" or "daily_quota" while open
        self._trial_running = False

    @property
    def state(self) -> BreakerState:
        if self.open_until is None:
            return "closed"
        if self._clock() < self.open_until:
            return "open"
        return "half_open"

    def allow(self) -> bool:
        """True when a call may start. In `half_open`, only one trial call starts."""
        state = self.state
        if state == "closed":
            return True
        if state == "open" or self._trial_running:
            return False
        self._trial_running = True
        return True

    def record_success(self) -> None:
        self.failures = 0
        self.open_until = None
        self.reason = ""
        self._trial_running = False

    def record_failure(self, until: datetime | None = None) -> None:
        """One failure. `until` opens the breaker at once until that time (daily quota)."""
        trial = self._trial_running
        self._trial_running = False
        self.failures += 1
        if until is not None:
            self.open_until = until
            self.reason = "daily_quota"
        elif trial or self.failures >= self.threshold:
            self.open_until = self._clock() + timedelta(seconds=self.open_seconds)
            self.reason = "failures"

    def release(self) -> None:
        """A trial call ended without an answer about the service (for example a limit of the
        turn stopped it). The next call can be the trial."""
        self._trial_running = False


class BreakerBoard:
    """The breakers of one process: one for each Gemini model and one for BigQuery."""

    def __init__(
        self,
        threshold: int = 3,
        open_seconds: float = 60.0,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.threshold = threshold
        self.open_seconds = open_seconds
        self.clock = clock
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(self, name: str) -> CircuitBreaker:
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(
                name, self.threshold, self.open_seconds, self.clock
            )
        return self._breakers[name]

    def states(self) -> dict[str, BreakerState]:
        return {name: b.state for name, b in self._breakers.items()}
