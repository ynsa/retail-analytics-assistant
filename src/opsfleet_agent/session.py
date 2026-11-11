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
from opsfleet_agent.identity import UserContext
from opsfleet_agent.limits import DailyLedger, TurnBudget
from opsfleet_agent.llm import GeminiClient
from opsfleet_agent.schema import load_schema, schema_to_text
from opsfleet_agent.store.db import connect
from opsfleet_agent.tracing import current_trace_id, init_tracing, span


@dataclass(frozen=True)
class TurnResult:
    answer: str
    error_type: str | None
    trace_id: str | None
    totals: dict[str, float | int]


def build_services(config: AppConfig, refresh_schema: bool = False) -> Services:
    """Create the real Gemini and BigQuery clients. Reading the table schemas is free."""
    from opsfleet_agent.bq_client import BigQueryRunner  # the client's file, unchanged

    settings = config.settings
    runner = BigQueryRunner(
        project_id=config.secrets.google_cloud_project,
        dataset_id=settings.bigquery.dataset,
    )
    schema = load_schema(
        runner,
        settings.bigquery.tables,
        settings.data_path("schema_cache.json"),
        refresh_schema,
    )
    return Services(
        settings=settings,
        llm=GeminiClient(settings.models, config.secrets.google_api_key),
        bq=BigQueryService(runner.client, settings.bigquery, settings.limits.per_query),
        schema_text=schema_to_text(schema, settings.bigquery.dataset),
        prompts=load_prompts(),
    )


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
        self.conversation_id = uuid.uuid4().hex
        with self.conn:
            self.conn.execute(
                "INSERT INTO conversations (id, user_id, started_at) VALUES (?, ?, ?)",
                (self.conversation_id, user.id, datetime.now(UTC).isoformat()),
            )

    def ask(self, question: str) -> TurnResult:
        budget = TurnBudget(self.config.settings.limits, self.ledger, kind="question")
        context = TurnContext(services=self.services, budget=budget, user=self.user)
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
