"""BigQuery wrapper around the client's BigQueryRunner (specs 00 and 36).

`bq_client.py` stays unchanged. `BigQueryRunner.execute_query()` accepts no job settings, so this
wrapper uses `runner.client` with a `QueryJobConfig`: a free dry run, `maximum_bytes_billed`,
a timeout, query parameters, job labels and a row cap.

Resilience (spec 36):

- The library retries are off: `job_retry=None`, and the API call retry stops after 10 s.
- The dry run and the query get 1 retry, only for a temporary error (`backendError`,
  `internalError`, `rateLimitExceeded`, HTTP 500 and 503, a connection error).
- The job timeout is `min(30 s, time left)`. A job that passes it, or a Ctrl-C, cancels the job.
- A circuit breaker: 3 temporary failures in a row stop all queries for 60 s.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pandas as pd
from google.cloud import bigquery
from google.cloud.bigquery.retry import DEFAULT_RETRY
from opentelemetry.trace import Span

from opsfleet_agent.config import BigQuerySettings, PerQueryLimits, ResilienceSettings
from opsfleet_agent.errors import AgentError, ErrorType, LimitExceeded
from opsfleet_agent.limits import TurnBudget
from opsfleet_agent.resilience.breaker import CircuitBreaker
from opsfleet_agent.resilience.errors_map import bq_down, is_timeout, map_bq_error
from opsfleet_agent.resilience.policies import (
    RetryHooks,
    RetryPolicy,
    ServiceFailure,
    run_with_retries,
)
from opsfleet_agent.tracing import current_trace_id, span

API_RETRY_TIMEOUT_S = (
    10.0  # the retry of one API call inside the library (spec 36, B2.2)
)
BREAKER_NAME = "bigquery"


@dataclass(frozen=True)
class QueryResult:
    dataframe: pd.DataFrame
    row_count: int
    truncated: bool
    bytes_processed: int
    bytes_billed: int
    job_id: str | None
    duration_ms: float


def _label(value: str) -> str:
    """BigQuery labels allow lowercase letters, digits, - and _ (max 63 characters)."""
    return re.sub(r"[^a-z0-9_-]", "_", value.lower())[:63]


def classify_bq_error(exc: BaseException) -> AgentError:
    """One error type and one user message (spec 36, B2.5)."""
    return map_bq_error(exc)


def api_retry() -> Any:
    """The retry of one API call: at most 10 s, so it never breaks the time of the turn."""
    return DEFAULT_RETRY.with_timeout(API_RETRY_TIMEOUT_S)


class BigQueryService:
    def __init__(
        self,
        client: Any,
        bq_settings: BigQuerySettings,
        limits: PerQueryLimits,
        *,
        resilience: ResilienceSettings | None = None,
        breaker: CircuitBreaker | None = None,
        hooks: RetryHooks | None = None,
    ) -> None:
        """`client` is `BigQueryRunner(...).client`, or a fake in the tests."""
        self._client = client
        self._settings = bq_settings
        self._limits = limits
        self._resilience = resilience or ResilienceSettings()
        self.breaker = breaker or CircuitBreaker(
            BREAKER_NAME,
            self._resilience.breaker_threshold,
            self._resilience.breaker_open_s,
        )
        self.hooks = hooks or RetryHooks()
        self.policy = RetryPolicy(
            retries=self._resilience.bq_retries,
            waits_s=tuple(self._resilience.retry_waits_s),
            jitter=self._resilience.retry_jitter,
            retry_after_max_s=self._resilience.retry_after_max_s,
        )
        self._running: Any | None = None  # the job that runs now, for Ctrl-C

    # --- state for the graph and the CLI ---------------------------------------------------------
    def available(self) -> bool:
        """False while the breaker is open. The graph checks it before `write_sql` (B2.4)."""
        return self.breaker.state != "open"

    def cancel_running(self) -> bool:
        """Cancel the running job (Ctrl-C). True when a job was cancelled."""
        job = self._running
        if job is None:
            return False
        with contextlib.suppress(Exception):  # the cancel is best effort
            job.cancel()
        self._running = None
        return True

    # --- query -----------------------------------------------------------------------------------
    def _config(
        self,
        params: list[Any] | None,
        labels: dict[str, str],
        dry_run: bool,
        job_timeout_s: float | None = None,
    ) -> bigquery.QueryJobConfig:
        config = bigquery.QueryJobConfig(
            dry_run=dry_run,
            use_query_cache=not dry_run,
            maximum_bytes_billed=self._limits.max_bytes_billed,
            query_parameters=params or [],
            labels=labels,
        )
        if job_timeout_s is not None:
            config.job_timeout_ms = int(job_timeout_s * 1000)
        return config

    def query(
        self,
        sql: str,
        budget: TurnBudget,
        *,
        params: list[Any] | None = None,
        user_id: str = "",
        purpose: str = "answer",
    ) -> QueryResult:
        labels = {"app": "opsfleet-agent", "purpose": _label(purpose)}
        if user_id:
            labels["user"] = _label(user_id)
        with span("bq.query", "sql", purpose=purpose) as sp:
            trace_id = current_trace_id()
            if trace_id:
                labels["trace_id"] = trace_id
            if not self.breaker.allow():
                sp.add_event("circuit_open", {"breaker": BREAKER_NAME})
                sp.set_attribute("circuit_open", True)
                raise bq_down("circuit open", circuit_open=True)
            try:
                estimate = self._retrying(
                    sp, budget, lambda: self._dry_run(sql, params, labels, budget)
                )
                sp.set_attribute("bytes_estimated", estimate)
                if estimate > self._limits.max_bytes_billed:
                    raise AgentError(
                        ErrorType.BQ_BYTES_CAP,
                        f"This query reads too much data (over {self._limits.max_bytes_billed / 1e9:g} GB). "
                        "Please narrow the period or the brands.",
                        f"dry run estimate {estimate} bytes",
                    )
                budget.charge_query()
                job = self._retrying(
                    sp, budget, lambda: self._run(sql, params, labels, budget)
                )
            except LimitExceeded:
                self.breaker.release()
                raise
            except ServiceFailure as exc:
                if exc.temporary:
                    self.breaker.record_failure()
                else:
                    self.breaker.record_success()  # BigQuery answered: a SQL error
                raise
            except AgentError:
                self.breaker.record_success()
                raise
            except BaseException:
                self.breaker.release()
                raise
            self.breaker.record_success()
            return self._result(sp, budget, *job)

    def _retrying(self, sp: Span, budget: TurnBudget, call: Callable[[], Any]) -> Any:
        min_left = self._resilience.min_time_left_s
        attempts = [0]

        def before(retry_no: int) -> None:
            attempts[0] = retry_no
            if retry_no:
                budget.check_time_left(min_left)

        def on_retry(retry_no: int, failure: ServiceFailure, wait: float) -> None:
            budget.check_time_left(min_left + wait)
            sp.add_event(
                "retry",
                {
                    "attempt": retry_no + 1,
                    "error_type": failure.error_type.value,
                    "kind": failure.kind,
                    "wait_s": round(wait, 2),
                },
            )

        return run_with_retries(
            lambda _n: call(),
            policy=self.policy,
            classify=lambda exc: map_bq_error(exc, self._limits.timeout_s),
            before_attempt=before,
            on_retry=on_retry,
            hooks=self.hooks,
        )

    def _dry_run(
        self,
        sql: str,
        params: list[Any] | None,
        labels: dict[str, str],
        budget: TurnBudget,
    ) -> int:
        timeout_s = min(
            self._resilience.bq_dry_run_timeout_s,
            max(1.0, budget.remaining_seconds()),
        )
        dry = self._client.query(
            sql,
            job_config=self._config(params, labels, dry_run=True),
            location=self._settings.location,
            retry=api_retry(),
            timeout=timeout_s,
            job_retry=None,
        )
        return int(dry.total_bytes_processed or 0)

    def _run(
        self,
        sql: str,
        params: list[Any] | None,
        labels: dict[str, str],
        budget: TurnBudget,
    ) -> tuple[Any, pd.DataFrame]:
        timeout_s = min(self._limits.timeout_s, max(1.0, budget.remaining_seconds()))
        job = self._client.query(
            sql,
            job_config=self._config(
                params, labels, dry_run=False, job_timeout_s=timeout_s
            ),
            location=self._settings.location,
            retry=api_retry(),
            job_retry=None,
        )
        self._running = job
        try:
            rows = job.result(
                timeout=timeout_s,
                max_results=self._limits.max_rows_to_llm + 1,
                job_retry=None,
            )
            frame = rows.to_dataframe()
        except BaseException as exc:
            if is_timeout(exc) or not isinstance(exc, Exception):
                # A timeout or Ctrl-C: stop the job, so it bills nothing more.
                with contextlib.suppress(Exception):
                    job.cancel()
            if isinstance(exc, Exception) and is_timeout(exc):
                raise map_bq_error(exc, self._limits.timeout_s) from exc
            raise
        finally:
            self._running = None
        return job, frame

    def _result(
        self, sp: Span, budget: TurnBudget, job: Any, frame: pd.DataFrame
    ) -> QueryResult:
        billed = int(job.total_bytes_billed or 0)
        budget.record_bytes(billed)
        truncated = len(frame) > self._limits.max_rows_to_llm
        frame = frame.head(self._limits.max_rows_to_llm)
        duration_ms = 0.0
        if job.started and job.ended:
            duration_ms = (job.ended - job.started).total_seconds() * 1000
        for key, value in {
            "job_id": job.job_id,
            "bytes_billed": billed,
            "bytes_processed": int(job.total_bytes_processed or 0),
            "rows": len(frame),
            "truncated": truncated,
        }.items():
            sp.set_attribute(key, value)
        return QueryResult(
            dataframe=frame,
            row_count=len(frame),
            truncated=truncated,
            bytes_processed=int(job.total_bytes_processed or 0),
            bytes_billed=billed,
            job_id=job.job_id,
            duration_ms=duration_ms,
        )
