"""Cost limits for each turn and for each day (spec 00, A4 and B2).

The code checks the budget before each Gemini call and each BigQuery query.
A prompt cannot enforce a limit, so the limits live here.

The day of the daily counter is the date in `limits.per_day.reset_timezone` (default
`America/Los_Angeles`), because Google resets the Gemini free-tier quota at midnight Pacific time.
Rows that an earlier version wrote with the UTC date stay as they are (spec 00, note of 2026-11-12).
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from datetime import time as day_time
from typing import Literal
from zoneinfo import ZoneInfo

from opsfleet_agent.config import Limits
from opsfleet_agent.errors import LimitExceeded
from opsfleet_agent.store.db import retry_locked

TurnKind = Literal["question", "report"]
RESET_NOTE = (
    "The limit resets at midnight Pacific time. "
    "A developer can change it in config/settings.yaml."
)


def ledger_day(now: datetime, timezone: str) -> str:
    """The key of the daily counter: the date in the reset time zone."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d")


def next_reset(now: datetime, timezone: str) -> datetime:
    """The next midnight in the reset time zone, as a UTC time."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    zone = ZoneInfo(timezone)
    local = now.astimezone(zone)
    midnight = datetime.combine(local.date() + timedelta(days=1), day_time(0), zone)
    return midnight.astimezone(UTC)


class DailyLedger:
    """Counts the Gemini calls and the BigQuery bytes of each day in `usage_daily`."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        limits: Limits,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._limits = limits.per_day
        self._clock = clock or (lambda: datetime.now(UTC))

    def day(self) -> str:
        """The key of the current day in `usage_daily`."""
        return ledger_day(self._clock(), self._limits.reset_timezone)

    def today(self) -> dict[str, int]:
        day = self.day()
        row = self._conn.execute(
            "SELECT * FROM usage_daily WHERE day = ?", (day,)
        ).fetchone()
        if row is None:
            return {
                "llm_calls": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "bq_queries": 0,
                "bq_bytes_billed": 0,
            }
        return {
            k: row[k]
            for k in (
                "llm_calls",
                "tokens_in",
                "tokens_out",
                "bq_queries",
                "bq_bytes_billed",
            )
        }

    def check_llm(self) -> None:
        if self.today()["llm_calls"] >= self._limits.max_llm_calls:
            raise LimitExceeded(
                "per_day.max_llm_calls",
                f"The daily limit of model calls ({self._limits.max_llm_calls}) is reached. "
                + RESET_NOTE,
            )

    def check_bq(self) -> None:
        if self.today()["bq_bytes_billed"] >= self._limits.max_bq_bytes_billed:
            gb = self._limits.max_bq_bytes_billed / 1e9
            raise LimitExceeded(
                "per_day.max_bq_bytes_billed",
                f"The daily limit of BigQuery data ({gb:g} GB) is reached. "
                + RESET_NOTE,
            )

    def add(
        self,
        *,
        llm_calls: int = 0,
        tokens_in: int = 0,
        tokens_out: int = 0,
        bq_queries: int = 0,
        bq_bytes_billed: int = 0,
    ) -> None:
        day = self.day()

        def write() -> None:
            with self._conn:
                self._conn.execute(
                    """
                INSERT INTO usage_daily (day, llm_calls, tokens_in, tokens_out, bq_queries, bq_bytes_billed)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(day) DO UPDATE SET
                    llm_calls = llm_calls + excluded.llm_calls,
                    tokens_in = tokens_in + excluded.tokens_in,
                    tokens_out = tokens_out + excluded.tokens_out,
                    bq_queries = bq_queries + excluded.bq_queries,
                    bq_bytes_billed = bq_bytes_billed + excluded.bq_bytes_billed
                """,
                    (
                        day,
                        llm_calls,
                        tokens_in,
                        tokens_out,
                        bq_queries,
                        bq_bytes_billed,
                    ),
                )

        retry_locked(write)  # spec 36, B2.2: one retry on "database is locked"


class TurnBudget:
    """The budget of one answer. Create a new one for each user turn."""

    def __init__(
        self,
        limits: Limits,
        ledger: DailyLedger,
        kind: TurnKind = "question",
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limits = limits.per_turn
        self._ledger = ledger
        self._monotonic = monotonic
        self._start = monotonic()
        self.kind = kind
        self.max_seconds = (
            self._limits.max_seconds_report
            if kind == "report"
            else self._limits.max_seconds_question
        )
        self.reserved_llm_calls = (
            0  # calls that only the last steps of a report may use
        )
        self.llm_calls = 0
        self.llm_attempts = (
            0  # spec 36: retries and fallback attempts after the first one
        )
        self.fallback_calls = 0  # spec 36: calls that the fallback model answered
        self.bq_queries = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.bytes_billed = 0

    def use_report_limits(self) -> None:
        """A report gets the report time limit (spec 00, A4). The route decides it after the
        budget is made, so the kind can change once. The time counts from the start of the turn."""
        self.kind = "report"
        self.max_seconds = self._limits.max_seconds_report

    def reserve_llm_calls(self, count: int) -> None:
        """Keep `count` model calls for the last steps of a report (spec 32, B2.1). Query
        corrections cannot use them. Call it again with 0 before the reserved calls."""
        self.reserved_llm_calls = max(0, count)

    def llm_calls_left(self) -> int:
        return max(0, self._limits.max_llm_calls - self.llm_calls)

    # --- time -------------------------------------------------------------------------------
    def elapsed(self) -> float:
        return self._monotonic() - self._start

    def remaining_seconds(self) -> float:
        return max(0.0, self.max_seconds - self.elapsed())

    def check_time(self) -> None:
        if self.elapsed() >= self.max_seconds:
            self._time_exceeded()

    def check_time_left(self, min_seconds: float) -> None:
        """Spec 36, B2.2: no attempt starts when less than `min_seconds` are left in the turn.
        The turn then ends with the time message."""
        if self.remaining_seconds() < min_seconds:
            self._time_exceeded()

    def _time_exceeded(self) -> None:
        if self.kind == "report":
            raise LimitExceeded(
                "per_turn.max_seconds_report",
                f"The report took longer than {self.max_seconds:g} s. "
                "Please ask for a shorter period or fewer topics.",
            )
        raise LimitExceeded(
            "per_turn.max_seconds",
            f"The answer took longer than {self.max_seconds:g} s. Please ask a narrower question.",
        )

    # --- Gemini -----------------------------------------------------------------------------
    def charge_llm(self) -> None:
        """Call before each Gemini call. Raises LimitExceeded when a limit is reached."""
        self.check_time()
        if self.llm_calls >= self._limits.max_llm_calls - self.reserved_llm_calls:
            raise LimitExceeded(
                "per_turn.max_llm_calls",
                f"This question needs more steps than allowed ({self._limits.max_llm_calls} model calls). "
                "Please ask a narrower question.",
            )
        if (
            self.tokens_in >= self._limits.max_input_tokens
            or self.tokens_out >= self._limits.max_output_tokens
        ):
            raise LimitExceeded(
                "per_turn.max_tokens", "This question needs more text than allowed."
            )
        self._ledger.check_llm()
        self.llm_calls += 1
        self._ledger.add(llm_calls=1)

    def charge_attempt(self) -> None:
        """Spec 36, B2.2: a retry or a fallback attempt of a Gemini call. The free tier counts
        each request, so the daily ledger counts it. The turn limit counts the logical call once."""
        self.check_time()
        self._ledger.check_llm()
        self.llm_attempts += 1
        self._ledger.add(llm_calls=1)

    def record_tokens(self, tokens_in: int, tokens_out: int) -> None:
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self._ledger.add(tokens_in=tokens_in, tokens_out=tokens_out)

    # --- BigQuery ---------------------------------------------------------------------------
    def charge_query(self) -> None:
        """Call before each BigQuery query (not before a free dry run)."""
        self.check_time()
        if self.bq_queries >= self._limits.max_bq_queries:
            raise LimitExceeded(
                "per_turn.max_bq_queries",
                f"This question needs more queries than allowed ({self._limits.max_bq_queries}).",
            )
        self._ledger.check_bq()
        self.bq_queries += 1
        self._ledger.add(bq_queries=1)

    def queries_left(self) -> int:
        """Queries that this turn can still run (the empty-result checks stop at 0)."""
        return max(0, self._limits.max_bq_queries - self.bq_queries)

    def record_bytes(self, bytes_billed: int) -> None:
        self.bytes_billed += bytes_billed
        self._ledger.add(bq_bytes_billed=bytes_billed)

    def totals(self) -> dict[str, float | int]:
        return {
            "llm_calls": self.llm_calls,
            "llm_attempts": self.llm_calls + self.llm_attempts,  # spec 36: each request
            "fallback_calls": self.fallback_calls,
            "bq_queries": self.bq_queries,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "bytes_billed": self.bytes_billed,
            "seconds": round(self.elapsed(), 2),
        }
