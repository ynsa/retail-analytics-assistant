"""BigQuery wrapper around the client's BigQueryRunner (spec 00).

`bq_client.py` stays unchanged. `BigQueryRunner.execute_query()` accepts no job settings, so this
wrapper uses `runner.client` with a `QueryJobConfig`: a free dry run, `maximum_bytes_billed`,
a timeout, query parameters, job labels and a row cap.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import re
from dataclasses import dataclass
from typing import Any

import pandas as pd
from google.api_core import exceptions as gexc
from google.cloud import bigquery

from opsfleet_agent.config import BigQuerySettings, PerQueryLimits
from opsfleet_agent.errors import AgentError, ErrorType
from opsfleet_agent.limits import TurnBudget
from opsfleet_agent.tracing import current_trace_id, span


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
    if isinstance(exc, AgentError):
        return exc
    text = str(exc)
    if _is_timeout(exc):
        return AgentError(
            ErrorType.BQ_TIMEOUT, "The data query took too long and was stopped.", text
        )
    if "bytes billed" in text.lower() or "bytesbilled" in text.lower():
        return AgentError(
            ErrorType.BQ_BYTES_CAP,
            "This query reads too much data. Please narrow the period or the brands.",
            text,
        )
    if isinstance(exc, gexc.NotFound):
        return AgentError(
            ErrorType.BQ_NOT_FOUND,
            "The query uses a table or a column that does not exist.",
            text,
        )
    if isinstance(exc, gexc.Forbidden):
        return AgentError(
            ErrorType.BQ_PERMISSION, "The data service refused access.", text
        )
    if isinstance(exc, gexc.BadRequest):
        return AgentError(ErrorType.BQ_SYNTAX, "The query has an error.", text)
    return AgentError(
        ErrorType.BQ_ERROR,
        "The data service returned an error.",
        f"{type(exc).__name__}: {text}",
    )


class BigQueryService:
    def __init__(
        self, client: Any, bq_settings: BigQuerySettings, limits: PerQueryLimits
    ) -> None:
        """`client` is `BigQueryRunner(...).client`, or a fake in the tests."""
        self._client = client
        self._settings = bq_settings
        self._limits = limits

    def _config(
        self, params: list[Any] | None, labels: dict[str, str], dry_run: bool
    ) -> bigquery.QueryJobConfig:
        return bigquery.QueryJobConfig(
            dry_run=dry_run,
            use_query_cache=not dry_run,
            maximum_bytes_billed=self._limits.max_bytes_billed,
            query_parameters=params or [],
            labels=labels,
        )

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
            try:
                dry = self._client.query(
                    sql,
                    job_config=self._config(params, labels, dry_run=True),
                    location=self._settings.location,
                )
                estimate = int(dry.total_bytes_processed or 0)
            except Exception as exc:
                raise classify_bq_error(exc) from exc
            sp.set_attribute("bytes_estimated", estimate)
            if estimate > self._limits.max_bytes_billed:
                raise AgentError(
                    ErrorType.BQ_BYTES_CAP,
                    f"This query reads too much data (over {self._limits.max_bytes_billed / 1e9:g} GB). "
                    "Please narrow the period or the brands.",
                    f"dry run estimate {estimate} bytes",
                )
            budget.charge_query()
            timeout_s = min(
                self._limits.timeout_s, max(1.0, budget.remaining_seconds())
            )
            job = None
            try:
                job = self._client.query(
                    sql,
                    job_config=self._config(params, labels, dry_run=False),
                    location=self._settings.location,
                    job_retry=None,
                )
                rows = job.result(
                    timeout=timeout_s, max_results=self._limits.max_rows_to_llm + 1
                )
                frame = rows.to_dataframe()
            except Exception as exc:
                if job is not None and _is_timeout(exc):
                    with contextlib.suppress(Exception):  # the cancel is best effort
                        job.cancel()
                raise classify_bq_error(exc) from exc
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


def _is_timeout(exc: BaseException) -> bool:
    return (
        isinstance(exc, (concurrent.futures.TimeoutError, TimeoutError))
        or "Timeout" in type(exc).__name__
    )
