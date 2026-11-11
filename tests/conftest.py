"""Shared fixtures. Unit tests never use the network: Gemini and BigQuery are fakes."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from opsfleet_agent import tracing
from opsfleet_agent.bq import QueryResult
from opsfleet_agent.config import AppConfig, Paths, load_config
from opsfleet_agent.limits import DailyLedger, TurnBudget
from opsfleet_agent.llm import LlmResponse
from opsfleet_agent.store.db import connect


@pytest.fixture(autouse=True)
def _fresh_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test gets its own tracer, so traces go to the tmp folder of the test."""
    monkeypatch.setattr(tracing, "_provider", None)


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    cfg = load_config()
    settings = cfg.settings.model_copy(
        update={"paths": Paths(data_dir=tmp_path / "data")}
    )
    return cfg.model_copy(update={"settings": settings})


@pytest.fixture
def conn(config: AppConfig):
    c = connect(config.settings.data_path("app.db"))
    yield c
    c.close()


@pytest.fixture
def ledger(config: AppConfig, conn) -> DailyLedger:
    return DailyLedger(conn, config.settings.limits)


@pytest.fixture
def budget(config: AppConfig, ledger: DailyLedger) -> TurnBudget:
    return TurnBudget(config.settings.limits, ledger)


@dataclass
class FakeLLM:
    """Returns the queued replies in order. A queued exception is raised instead."""

    replies: list[Any] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def generate(
        self,
        budget: TurnBudget,
        *,
        system: str,
        prompt: str,
        schema=None,
        purpose: str = "",
        temperature: float = 0.0,
    ) -> LlmResponse:
        budget.charge_llm()
        self.calls.append({"purpose": purpose, "prompt": prompt})
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if schema is not None:
            parsed = schema.model_validate(reply)
            return LlmResponse(
                text="", parsed=parsed, tokens_in=100, tokens_out=20, model="fake"
            )
        budget.record_tokens(100, 20)
        return LlmResponse(
            text=reply, parsed=None, tokens_in=100, tokens_out=20, model="fake"
        )


@dataclass
class FakeBQ:
    results: list[Any] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)

    def query(self, sql: str, budget: TurnBudget, **_: Any) -> QueryResult:
        budget.charge_query()
        self.queries.append(sql)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def frame_result(frame: pd.DataFrame) -> QueryResult:
    return QueryResult(
        dataframe=frame,
        row_count=len(frame),
        truncated=False,
        bytes_processed=1000,
        bytes_billed=10_485_760,
        job_id="job-1",
        duration_ms=5.0,
    )
