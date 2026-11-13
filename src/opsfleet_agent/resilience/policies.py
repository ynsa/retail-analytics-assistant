"""Retry policies with fixed limits (spec 36, B2.2).

One loop does all retries of the assistant. The SDK retries are off, so the limits stay clear:

- at most `retries` more attempts, only for a temporary failure,
- a wait of 1 s, then 2 s, each with a random part of up to 50%,
- a `Retry-After` of up to 10 s replaces the wait. A longer one stops the retries,
- no attempt starts when the turn has less than 5 s left (`LimitExceeded`, the time message).
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass

from opsfleet_agent.errors import AgentError, ErrorType


class ServiceFailure(AgentError):
    """A classified failure of Gemini or BigQuery.

    - `temporary`: a retry can help (timeout, 429, 5xx, connection error).
    - `retry_after`: the wait that the service asked for, in seconds, or None.
    - `daily_quota`: the daily free-tier quota of the model is used up.
    - `kind`: a short name for the user message and the trace, for example `timeout`, `busy`,
      `server`, `key`, `billing`, `refused`, `other`.
    """

    def __init__(
        self,
        error_type: ErrorType,
        user_message: str,
        detail: str = "",
        *,
        kind: str = "other",
        temporary: bool = False,
        retry_after: float | None = None,
        daily_quota: bool = False,
        circuit_open: bool = False,
    ) -> None:
        super().__init__(error_type, user_message, detail)
        self.kind = kind
        self.temporary = temporary
        self.retry_after = retry_after
        self.daily_quota = daily_quota
        self.circuit_open = circuit_open


@dataclass(frozen=True)
class RetryPolicy:
    retries: int
    waits_s: tuple[float, ...] = (1.0, 2.0)
    jitter: float = 0.5
    retry_after_max_s: float = 10.0

    def wait(self, retry_no: int, retry_after: float | None, rand: float) -> float:
        """The wait before retry number `retry_no` (1, 2, ...)."""
        if retry_after is not None:
            return max(0.0, retry_after)
        base = self.waits_s[min(retry_no, len(self.waits_s)) - 1] if self.waits_s else 0
        return base * (1 + self.jitter * rand)


_sleep: Callable[[float], None] = time.sleep  # the unit tests replace it: no real wait


def default_sleep(seconds: float) -> None:
    _sleep(seconds)


@dataclass
class RetryHooks:
    """The parts of a retry loop that tests replace: the sleep and the random part."""

    sleep: Callable[[float], None] = default_sleep
    rand: Callable[[], float] = random.random


def run_with_retries[T](
    attempt: Callable[[int], T],
    *,
    policy: RetryPolicy,
    classify: Callable[[BaseException], ServiceFailure],
    before_attempt: Callable[[int], None],
    on_retry: Callable[[int, ServiceFailure, float], None],
    hooks: RetryHooks,
) -> T:
    """Run `attempt(n)` for n = 0, 1, ... until it succeeds or the policy stops.

    `before_attempt(n)` runs before each attempt. It checks the time and the budget, and raises
    to stop. `on_retry(n, failure, wait)` runs before the wait of retry n (a span event). The last
    failure is raised as a `ServiceFailure`.
    """
    retry_no = 0
    while True:
        before_attempt(retry_no)
        try:
            return attempt(retry_no)
        except ServiceFailure:
            raise
        except AgentError:
            raise  # a limit or another error of the assistant: no retry
        except Exception as exc:
            failure = classify(exc)
            if not _may_retry(failure, retry_no, policy):
                raise failure from exc
            wait = policy.wait(retry_no + 1, failure.retry_after, hooks.rand())
            on_retry(retry_no + 1, failure, wait)
            hooks.sleep(wait)
            retry_no += 1


def _may_retry(failure: ServiceFailure, retry_no: int, policy: RetryPolicy) -> bool:
    if not failure.temporary or failure.daily_quota or retry_no >= policy.retries:
        return False
    return (
        failure.retry_after is None or failure.retry_after <= policy.retry_after_max_s
    )
