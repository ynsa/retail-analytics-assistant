"""Recorders around the Gemini and BigQuery services of spec 00 (spec 23, B2.3).

Both wrappers call the real service, so the budget and the trace stay active. They also:
- store each SQL, its parameters, the result (at most 200 rows) and each model input and output;
- enforce the hard cap of the run and the call interval (one Gemini call every N seconds);
- run the `inject_row_text` fixture (spec 22).
"""

from __future__ import annotations

import dataclasses
import math
import time
from collections.abc import Callable
from typing import Any

import pandas as pd

from opsfleet_agent.errors import AgentError, LimitExceeded
from opsfleet_agent.evals.fixtures import inject_row_text
from opsfleet_agent.evals.records import LlmRecord, QueryRecord
from opsfleet_agent.evals.snapshot import plain

MAX_RECORDED_ROWS = 200


class RunCapReached(LimitExceeded):
    def __init__(self, cap: int) -> None:
        super().__init__(
            "eval.run_cap", f"The evaluation run reached its cap of {cap} calls."
        )
        self.cap = cap


class CallCap:
    """The hard cap of a run: planned calls plus the margin. Agent and judge calls both count."""

    def __init__(self, cap: int, used: int = 0) -> None:
        self.cap = cap
        self.used = used

    @property
    def reached(self) -> bool:
        return self.used >= self.cap

    def left(self) -> int:
        return max(0, self.cap - self.used)

    def check(self) -> None:
        if self.reached:
            raise RunCapReached(self.cap)

    def add(self, n: int = 1) -> None:
        self.used += n


class Throttle:
    """At most one Gemini call every `interval_s` seconds (free-tier rate limit)."""

    def __init__(
        self,
        interval_s: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._interval = interval_s
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            delay = self._interval - (self._clock() - self._last)
            if delay > 0:
                self._sleep(delay)
        self._last = self._clock()


class TurnSink:
    """Collects the records of the current turn."""

    def __init__(self) -> None:
        self.queries: list[QueryRecord] = []
        self.llm_calls: list[LlmRecord] = []

    def begin(self) -> None:
        self.queries, self.llm_calls = [], []

    def take(self) -> tuple[list[QueryRecord], list[LlmRecord]]:
        out = (self.queries, self.llm_calls)
        self.begin()
        return out


class RecordingGeminiClient:
    """Wraps `GeminiClient`. `mirror` adds the calls to a second ledger (the real daily counter)."""

    def __init__(
        self,
        inner: Any,
        sink: TurnSink,
        cap: CallCap,
        throttle: Throttle,
        mirror: Any | None = None,
        model: str = "",
    ) -> None:
        self._inner = inner
        self._sink = sink
        self._cap = cap
        self._throttle = throttle
        self._mirror = mirror
        self.model = model

    def generate(self, budget: Any, *, system: str, prompt: str, **kwargs: Any) -> Any:
        self._cap.check()
        self._throttle.wait()
        start = time.monotonic()
        record = LlmRecord(
            purpose=kwargs.get("purpose", "generate"),
            model=self.model,
            system=system,
            prompt=prompt,
            output="",
        )
        self._cap.add(1)
        if self._mirror is not None:
            self._mirror.add(llm_calls=1)
        try:
            response = self._inner.generate(
                budget, system=system, prompt=prompt, **kwargs
            )
        except AgentError as exc:
            record.error_type = exc.error_type.value
            record.latency_ms = (time.monotonic() - start) * 1000
            self._sink.llm_calls.append(record)
            raise
        record.output = response.text or (
            response.parsed.model_dump_json() if response.parsed is not None else ""
        )
        record.model = response.model or self.model
        record.tokens_in, record.tokens_out = response.tokens_in, response.tokens_out
        record.latency_ms = (time.monotonic() - start) * 1000
        if self._mirror is not None:
            self._mirror.add(
                tokens_in=response.tokens_in, tokens_out=response.tokens_out
            )
        self._sink.llm_calls.append(record)
        return response


def _params_dict(params: list[Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for p in params or []:
        name = getattr(p, "name", None) or str(len(out))
        out[name] = plain(getattr(p, "values", getattr(p, "value", None)))
        if isinstance(out[name], tuple):
            out[name] = list(out[name])
    return out


def _rows(frame: pd.DataFrame) -> list[list[Any]]:
    rows = frame.head(MAX_RECORDED_ROWS).astype(object).values.tolist()
    return [
        [None if isinstance(v, float) and math.isnan(v) else plain(v) for v in r]
        for r in rows
    ]


class RecordingBigQueryService:
    """Wraps `BigQueryService`. With `inject_text`, it runs the fixture `inject_row_text`."""

    def __init__(self, inner: Any, sink: TurnSink, mirror: Any | None = None) -> None:
        self._inner = inner
        self._sink = sink
        self._mirror = mirror
        self.inject_text: str | None = None

    def query(self, sql: str, budget: Any, **kwargs: Any) -> Any:
        record = QueryRecord(sql=sql, params=_params_dict(kwargs.get("params")))
        record.purpose = kwargs.get("purpose", "")
        try:
            result = self._inner.query(sql, budget, **kwargs)
        except AgentError as exc:
            record.error_type = exc.error_type.value
            self._sink.queries.append(record)
            raise
        frame = result.dataframe
        if self.inject_text and len(frame):
            columns = [str(c) for c in frame.columns]
            new_rows = inject_row_text(
                columns, frame.astype(object).values.tolist(), self.inject_text
            )
            frame = pd.DataFrame(new_rows, columns=frame.columns)
            result = dataclasses.replace(result, dataframe=frame)
        record.columns = [str(c) for c in frame.columns]
        record.rows = _rows(frame)
        record.bytes_billed = int(getattr(result, "bytes_billed", 0) or 0)
        if self._mirror is not None:
            self._mirror.add(bq_queries=1, bq_bytes_billed=record.bytes_billed)
        self._sink.queries.append(record)
        return result
