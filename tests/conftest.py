"""Shared fixtures. Unit tests never use the network: Gemini and BigQuery are fakes."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from opsfleet_agent import tracing
from opsfleet_agent.bq import QueryResult
from opsfleet_agent.config import AppConfig, Paths, Role, load_config
from opsfleet_agent.guard.value_cache import ValueCache
from opsfleet_agent.identity import UserContext
from opsfleet_agent.limits import DailyLedger, TurnBudget
from opsfleet_agent.llm import LlmResponse
from opsfleet_agent.schema import Schema
from opsfleet_agent.store.db import connect

FIXTURES = Path(__file__).parent / "fixtures"
DATASET = "bigquery-public-data.thelook_ecommerce"


def T(table: str) -> str:
    """The full table name in back-quotes (spec 30, B4)."""
    return f"`{DATASET}.{table}`"


def load_test_schema() -> Schema:
    return Schema.from_cache(
        json.loads((FIXTURES / "schema.json").read_text(encoding="utf-8")), DATASET
    )


def load_test_values() -> ValueCache:
    return ValueCache.from_json(
        (FIXTURES / "value_cache.json").read_text(encoding="utf-8")
    )


# "M" and "C" of spec 30, B4.
MANAGER = UserContext("m", "M (manager)", Role.MANAGER, ("Calvin Klein", "Levi's"))
CEO = UserContext("c", "C (ceo)", Role.CEO, ())


@pytest.fixture
def test_schema() -> Schema:
    return load_test_schema()


@pytest.fixture
def test_values() -> ValueCache:
    return load_test_values()


@pytest.fixture(autouse=True)
def _fresh_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test gets its own tracer, so traces go to the tmp folder of the test."""
    monkeypatch.setattr(tracing, "_provider", None)


@pytest.fixture(autouse=True)
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """No real wait in a unit test (spec 36, B4): the retry waits go to this list."""
    from opsfleet_agent.resilience import policies

    waits: list[float] = []
    monkeypatch.setattr(policies, "_sleep", waits.append)
    return waits


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
        **_: Any,
    ) -> LlmResponse:
        budget.charge_llm()
        self.calls.append({"purpose": purpose, "prompt": prompt, "system": system})
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
    params: list[list[Any]] = field(default_factory=list)
    purposes: list[str] = field(default_factory=list)

    def query(
        self,
        sql: str,
        budget: TurnBudget,
        *,
        params: list[Any] | None = None,
        purpose: str = "answer",
        **_: Any,
    ) -> QueryResult:
        budget.charge_query()
        self.queries.append(sql)
        self.params.append(list(params or []))
        self.purposes.append(purpose)
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


# --- The agent with fakes (spec 31) ---------------------------------------------------------------


def route_reply(intent: str = "analysis", **fields: Any) -> dict[str, Any]:
    """A route JSON for the fake model (spec 31, B2.2)."""
    return {"intent": intent, **fields}


def sql_reply(
    sql: str | None,
    *,
    final: bool = True,
    purpose: str = "Count the orders",
    period: str | None = "2026-10-01..2026-10-31",
    definitions: list[str] | None = None,
    assumptions: list[str] | None = None,
    cannot_answer: str | None = None,
) -> dict[str, Any]:
    """A write_sql JSON for the fake model (spec 31, B2.3)."""
    return {
        "purpose": purpose,
        "sql": sql,
        "final": final,
        "definitions_used": definitions if definitions is not None else ["orders"],
        "active_filters": {"period": period, "brands": [], "other": []},
        "assumptions": assumptions or [],
        "cannot_answer": cannot_answer,
    }


@pytest.fixture
def persona_file(tmp_path: Path) -> Path:
    path = tmp_path / "persona.md"
    path.write_text("Tone: friendly and direct.", encoding="utf-8")
    return path


@pytest.fixture
def make_session(config: AppConfig, conn, persona_file: Path):
    """Build an AssistantSession with a fake Gemini, a fake BigQuery and an in-memory checkpoint."""
    from langgraph.checkpoint.memory import InMemorySaver

    from opsfleet_agent.agent.context import PromptLibrary
    from opsfleet_agent.agent.state import Services
    from opsfleet_agent.identity import resolve_user
    from opsfleet_agent.session import AssistantSession

    def _make(
        user_id: str = "maya",
        llm: FakeLLM | None = None,
        bq: FakeBQ | None = None,
        values: ValueCache | None = None,
    ):
        llm = llm or FakeLLM()
        bq = bq or FakeBQ()
        services = Services(
            settings=config.settings,
            llm=llm,
            bq=bq,
            schema=load_test_schema(),
            values=values or load_test_values(),
            prompts=PromptLibrary(persona_path=persona_file),
        )
        session = AssistantSession(
            config,
            resolve_user(config.users, user_id),
            services,
            conn=conn,
            checkpointer=InMemorySaver(),
        )
        return session, llm, bq

    return _make


def read_spans(config: AppConfig) -> list[dict[str, Any]]:
    out = []
    for f in config.settings.data_path("traces").glob("*.jsonl"):
        out += [json.loads(line) for line in f.read_text().splitlines()]
    return out
