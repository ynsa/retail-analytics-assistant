"""Cost limits for each turn and for each UTC day (spec 00, A4 and B2).

The code checks the budget before each Gemini call and each BigQuery query.
A prompt cannot enforce a limit, so the limits live here.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from opsfleet_agent.config import Limits
from opsfleet_agent.errors import LimitExceeded

TurnKind = Literal["question", "report"]


def utc_day(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m-%d")


class DailyLedger:
    """Counts the Gemini calls and the BigQuery bytes of each UTC day in `usage_daily`."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        limits: Limits,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._limits = limits.per_day
        self._clock = clock or (lambda: datetime.now(UTC))

    def today(self) -> dict[str, int]:
        day = utc_day(self._clock())
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
                "The limit resets at midnight UTC. A developer can change it in config/settings.yaml.",
            )

    def check_bq(self) -> None:
        if self.today()["bq_bytes_billed"] >= self._limits.max_bq_bytes_billed:
            gb = self._limits.max_bq_bytes_billed / 1e9
            raise LimitExceeded(
                "per_day.max_bq_bytes_billed",
                f"The daily limit of BigQuery data ({gb:g} GB) is reached. "
                "The limit resets at midnight UTC. A developer can change it in config/settings.yaml.",
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
        day = utc_day(self._clock())
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
                (day, llm_calls, tokens_in, tokens_out, bq_queries, bq_bytes_billed),
            )


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
        self.llm_calls = 0
        self.bq_queries = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.bytes_billed = 0

    # --- time -------------------------------------------------------------------------------
    def elapsed(self) -> float:
        return self._monotonic() - self._start

    def remaining_seconds(self) -> float:
        return max(0.0, self.max_seconds - self.elapsed())

    def check_time(self) -> None:
        if self.elapsed() >= self.max_seconds:
            raise LimitExceeded(
                "per_turn.max_seconds",
                f"The answer took longer than {self.max_seconds:g} s. Please ask a narrower question.",
            )

    # --- Gemini -----------------------------------------------------------------------------
    def charge_llm(self) -> None:
        """Call before each Gemini call. Raises LimitExceeded when a limit is reached."""
        self.check_time()
        if self.llm_calls >= self._limits.max_llm_calls:
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

    def record_bytes(self, bytes_billed: int) -> None:
        self.bytes_billed += bytes_billed
        self._ledger.add(bq_bytes_billed=bytes_billed)

    def totals(self) -> dict[str, float | int]:
        return {
            "llm_calls": self.llm_calls,
            "bq_queries": self.bq_queries,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "bytes_billed": self.bytes_billed,
            "seconds": round(self.elapsed(), 2),
        }
