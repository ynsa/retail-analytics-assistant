"""AssistantSession: one conversation of one user (spec 37). The CLI and the UI only display.

The session builds the services once, creates a TurnBudget for each turn, runs the graph in a
`turn` span, and records the conversation in the database.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver

from opsfleet_agent.agent.graph import build_graph, load_prompts
from opsfleet_agent.agent.state import Services, TurnContext
from opsfleet_agent.bq import BigQueryService
from opsfleet_agent.config import AppConfig
from opsfleet_agent.errors import ConfigError
from opsfleet_agent.guard.empty_result import EmptyResultChecker
from opsfleet_agent.guard.masker import ResultMasker
from opsfleet_agent.guard.sql_guard import SqlGuard
from opsfleet_agent.guard.value_cache import (
    ValueCache,
    load_value_cache,
    validate_brands,
)
from opsfleet_agent.identity import UserContext
from opsfleet_agent.limits import DailyLedger, TurnBudget
from opsfleet_agent.llm import GeminiClient
from opsfleet_agent.schema import Schema, load_schema
from opsfleet_agent.store.db import connect
from opsfleet_agent.tracing import current_trace_id, init_tracing, span


@dataclass(frozen=True)
class TurnResult:
    answer: str
    error_type: str | None
    trace_id: str | None
    totals: dict[str, float | int]


def build_services(
    config: AppConfig, refresh_schema: bool = False, refresh_cache: bool = False
) -> Services:
    """Create the real Gemini and BigQuery clients. Reading the table schemas is free. The value
    cache (spec 30) costs one small query (about 40 MB billed) when it is missing or older than
    7 days. The query counts in the daily limit."""
    from opsfleet_agent.bq_client import BigQueryRunner  # the client's file, unchanged

    settings = config.settings
    runner = BigQueryRunner(
        project_id=config.secrets.google_cloud_project,
        dataset_id=settings.bigquery.dataset,
    )
    raw_schema = load_schema(
        runner,
        settings.bigquery.tables,
        settings.data_path("schema_cache.json"),
        refresh_schema,
    )
    bq = BigQueryService(runner.client, settings.bigquery, settings.limits.per_query)
    conn = connect(settings.data_path("app.db"))
    try:
        budget = TurnBudget(settings.limits, DailyLedger(conn, settings.limits))
        values = load_value_cache(
            settings.data_path("value_cache.json"),
            bq,
            budget,
            settings.bigquery.dataset,
            refresh=refresh_cache,
        )
    finally:
        conn.close()
    return Services(
        settings=settings,
        llm=GeminiClient(settings.models, config.secrets.google_api_key),
        bq=bq,
        schema=Schema.from_cache(raw_schema, settings.bigquery.dataset),
        values=values,
        prompts=load_prompts(),
        masker=ResultMasker(max_rows=settings.limits.per_query.max_rows_to_llm),
    )


def check_user_brands(config: AppConfig, values: ValueCache, user_id: str) -> list[str]:
    """Spec 30, B2.8: a wrong brand of this user stops the start (ConfigError). Wrong brands of
    other users give one warning line each (the returned list)."""
    problems = validate_brands(config.users, values)
    own = [p.message() for p in problems if p.user_id == user_id]
    if own:
        raise ConfigError(
            "\n".join(own) + " Correct the brand names in config/users.yaml."
        )
    return [p.message() for p in problems if p.user_id != user_id]


class AssistantSession:
    def __init__(
        self,
        config: AppConfig,
        user: UserContext,
        services: Services,
        conn: sqlite3.Connection | None = None,
        checkpointer: Any | None = None,
    ) -> None:
        self.config = config
        self.user = user
        self.services = services
        settings = config.settings
        init_tracing(settings.data_path("traces"))
        self.conn = conn or connect(settings.data_path("app.db"))
        self.ledger = DailyLedger(self.conn, settings.limits)
        if checkpointer is None:
            cp_conn = sqlite3.connect(
                settings.data_path("checkpoints.db"), check_same_thread=False
            )
            checkpointer = SqliteSaver(cp_conn)
        self.graph = build_graph(checkpointer)
        self.guard = SqlGuard(user, services.schema, services.values)
        self.empty_checker = EmptyResultChecker(
            self.guard, services.bq, services.values
        )
        self.conversation_id = uuid.uuid4().hex
        with self.conn:
            self.conn.execute(
                "INSERT INTO conversations (id, user_id, started_at) VALUES (?, ?, ?)",
                (self.conversation_id, user.id, datetime.now(UTC).isoformat()),
            )

    def ask(self, question: str) -> TurnResult:
        budget = TurnBudget(self.config.settings.limits, self.ledger, kind="question")
        context = TurnContext(
            services=self.services,
            budget=budget,
            user=self.user,
            guard=self.guard,
            empty_checker=self.empty_checker,
        )
        config = {"configurable": {"thread_id": self.conversation_id}}
        with span(
            "turn", "turn", user_id=self.user.id, conversation_id=self.conversation_id
        ) as sp:
            trace_id = current_trace_id()
            state = self.graph.invoke(
                {
                    "messages": [{"role": "user", "text": question}],
                    "user": self.user.as_state(),
                    "conversation_id": self.conversation_id,
                    "turn": {},
                },
                config,
                context=context,
            )
            turn = state.get("turn") or {}
            totals = budget.totals()
            for key, value in totals.items():
                sp.set_attribute(key, value)
            if turn.get("error_type"):
                sp.set_attribute("error_type", turn["error_type"])
        return TurnResult(
            answer=state["messages"][-1]["text"],
            error_type=turn.get("error_type"),
            trace_id=trace_id,
            totals=totals,
        )
