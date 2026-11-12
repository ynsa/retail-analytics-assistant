"""AssistantSession: one conversation of one user (spec 37). The CLI and the UI only display.

The session builds the services once, creates a TurnBudget for each turn, runs the graph in a
`turn` span, and records the conversation in the database.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from langgraph.checkpoint.sqlite import SqliteSaver

from opsfleet_agent.agent.context import PromptLibrary
from opsfleet_agent.agent.graph import build_graph
from opsfleet_agent.agent.state import Services, TurnContext
from opsfleet_agent.bq import BigQueryService
from opsfleet_agent.config import AppConfig, Settings
from opsfleet_agent.errors import AgentError, ConfigError, ReportNotFound
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
from opsfleet_agent.reports.files import display_path
from opsfleet_agent.reports.flow import build_report_flow, list_text
from opsfleet_agent.reports.library import ReportLibrary
from opsfleet_agent.schema import Schema, load_schema
from opsfleet_agent.store.db import connect
from opsfleet_agent.tracing import current_trace_id, init_tracing, span


@dataclass(frozen=True)
class TurnResult:
    """The result of one turn, for the CLI, the UI and the evaluation runner (spec 23)."""

    answer: str  # the final text after all checks, with the footer
    error_type: str | None  # spec 00 error type, None for a normal answer
    trace_id: str | None
    totals: dict[
        str, float | int
    ]  # llm_calls, bq_queries, tokens_in/out, bytes_billed, seconds
    intent: str | None = (
        None  # the route intent (spec 31, B2.2), None when no route ran
    )
    route: dict[str, Any] = field(default_factory=dict)  # the full route JSON
    steps: tuple[dict[str, Any], ...] = ()  # StepRecord of each query step
    executed_sql: tuple[str, ...] = ()  # the scoped SQL that BigQuery ran, in order
    footer: str = ""  # period, brands, definitions and assumptions
    unverified_figures: int = 0  # numbers that the figure check replaced
    calc_errors: int = 0  # placeholders that became "[not available]"
    calculated_figures: int = 0  # spec 32: figures that code computed from placeholders
    report_id: str | None = None  # spec 32: the saved report of this turn
    report_files: tuple[str, ...] = ()  # spec 32: files written in this turn


@dataclass(frozen=True)
class CommandOutcome:
    """The result of a slash command (spec 37, B3). Commands make no model call and no query."""

    kind: Literal["message", "error"]
    text: str
    path: str | None = None  # a written file, for /export


def build_prompts(settings: Settings) -> PromptLibrary:
    """The prompt files. The persona file is read again at each turn."""
    return PromptLibrary(
        persona_path=settings.project_path(settings.agent.persona_file),
        persona_max_chars=settings.agent.persona_max_chars,
    )


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
        prompts=build_prompts(settings),
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
        self.graph = build_graph(checkpointer, flows={"report": build_report_flow()})
        self.monotonic: Callable[[], float] = (
            time.monotonic
        )  # a fake clock in the tests
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
        budget = TurnBudget(
            self.config.settings.limits,
            self.ledger,
            kind="question",
            monotonic=self.monotonic,
        )
        context = TurnContext(
            services=self.services,
            budget=budget,
            user=self.user,
            guard=self.guard,
            empty_checker=self.empty_checker,
            store=self.conn,
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
            if turn.get("intent"):
                sp.set_attribute("intent", turn["intent"])
        return TurnResult(
            answer=state["messages"][-1]["text"],
            error_type=turn.get("error_type"),
            trace_id=trace_id,
            totals=totals,
            intent=turn.get("intent"),
            route=dict(turn.get("route") or {}),
            steps=tuple(dict(s) for s in turn.get("steps") or []),
            executed_sql=tuple(context.scratch.get("executed_sql") or ()),
            footer=turn.get("footer") or "",
            unverified_figures=int(turn.get("unverified_figures") or 0),
            calc_errors=int(turn.get("calc_errors") or 0),
            calculated_figures=int(
                ((turn.get("report") or {}).get("built") or {}).get(
                    "calculated_figures"
                )
                or 0
            ),
            report_id=(turn.get("report") or {}).get("saved_id"),
            report_files=tuple((turn.get("report") or {}).get("files") or ()),
        )

    # --- commands (no model call, no query) ---------------------------------------------------
    @property
    def library(self) -> ReportLibrary:
        return ReportLibrary(
            self.conn,
            self.config.settings,
            self.services.scanner,
            self.services.clock,
        )

    def command(self, name: str, args: list[str]) -> CommandOutcome:
        """`/reports`, `/open <id>`, `/export <id> --format html|md` (spec 32, A2.6 and A2.7)."""
        name = name.lstrip("/").lower()
        try:
            if name == "reports":
                listing = self.library.list(self.user)
                return CommandOutcome(
                    "message", list_text(self.services.prompts, listing)
                )
            if name == "open":
                if len(args) != 1:
                    return CommandOutcome("error", "Usage: /open <id>")
                return CommandOutcome(
                    "message", self.library.get(self.user, args[0]).body_markdown
                )
            if name == "export":
                fmt = _export_format(args)
                if fmt is None:
                    return CommandOutcome(
                        "error", "Usage: /export <id> --format html|md"
                    )
                path = display_path(self.library.export(self.user, args[0], fmt))
                return CommandOutcome("message", f"Saved: {path}", path=path)
        except ReportNotFound as exc:
            return CommandOutcome("error", exc.user_message)
        except AgentError as exc:
            return CommandOutcome("error", exc.user_message)
        return CommandOutcome("error", f"Unknown command '/{name}'. Type /help.")


def _export_format(args: list[str]) -> str | None:
    """`<id> --format html|md` (the format is html when it is not given)."""
    if not args:
        return None
    rest = args[1:]
    if not rest:
        return "html"
    if len(rest) == 2 and rest[0] == "--format" and rest[1] in ("html", "md"):
        return rest[1]
    return None
