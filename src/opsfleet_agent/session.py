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
from langgraph.types import Command

from opsfleet_agent.agent.context import PromptLibrary
from opsfleet_agent.agent.graph import build_graph
from opsfleet_agent.agent.state import Services, TurnContext
from opsfleet_agent.agent.tools import ToolRegistry
from opsfleet_agent.bq import BigQueryService
from opsfleet_agent.config import AppConfig, Settings
from opsfleet_agent.errors import AgentError, ConfigError, ErrorType, ReportNotFound
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
from opsfleet_agent.reports.delete_flow import build_delete_flow, undo_text
from opsfleet_agent.reports.deletion import DeleteReportsTool, undo_batch
from opsfleet_agent.reports.files import display_path
from opsfleet_agent.reports.flow import build_report_flow, list_text
from opsfleet_agent.reports.library import ReportLibrary
from opsfleet_agent.resilience import messages
from opsfleet_agent.resilience.checkpoint import GuardedSaver
from opsfleet_agent.resilience.safe_node import (
    init_logging,
    internal_message,
    log_exception,
)
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
    # spec 33: a delete that waits for the typed confirmation, and the outcome of a delete
    confirmation: ConfirmationView | None = None
    replay_input: bool = (
        False  # the text was not the confirmation: send it again as a turn
    )
    delete_outcome: str | None = (
        None  # deleted, wrong_number, not_exact, other_text, ...
    )
    batch_id: str | None = None  # the delete batch of a delete or an undo
    # spec 36: "ok", "error" or "cancelled" (Ctrl-C). `saved` is False when the checkpoint write
    # failed: the answer then ends with "This turn is not saved."
    status: str = "ok"
    saved: bool = True


@dataclass(frozen=True)
class ConfirmationView:
    """The waiting delete, from the `interrupt()` payload (spec 33, B2.3). The CLI shows it."""

    kind: str  # "delete_reports"
    rows: tuple[dict[str, Any], ...]  # report_id, title, created, reason, files
    count: int
    expected_text: str  # "delete 3"
    text: str  # the full list and the request to type the text
    not_found: tuple[str, ...] = ()


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
    bq = BigQueryService(
        runner.client,
        settings.bigquery,
        settings.limits.per_query,
        resilience=settings.resilience,
    )
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
        llm=GeminiClient(
            settings.models,
            config.secrets.google_api_key,
            resilience=settings.resilience,
            reset_timezone=settings.limits.per_day.reset_timezone,
        ),
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
    """One conversation of one user.

    `send(text)` is the only way to answer: when a delete waits for its confirmation
    (`pending` is set), `send()` resumes the paused graph with the text. Otherwise it starts a
    new turn. A `TurnResult` with `replay_input = True` means: the text was not the confirmation,
    the delete is cancelled, and the caller sends the same text again as a new turn (spec 33,
    A2.8). `cancel_pending()` cancels a waiting delete (`/exit`, Ctrl-C, Ctrl-D).
    """

    def __init__(
        self,
        config: AppConfig,
        user: UserContext,
        services: Services,
        conn: sqlite3.Connection | None = None,
        checkpointer: Any | None = None,
        conversation_id: str | None = None,
    ) -> None:
        self.config = config
        self.user = user
        self.services = services
        settings = config.settings
        init_tracing(settings.data_path("traces"))
        init_logging(settings.data_path("logs"))
        self.conn = conn or connect(
            settings.data_path("app.db"),
            busy_timeout_s=settings.resilience.sqlite_busy_timeout_s,
        )
        self.ledger = DailyLedger(self.conn, settings.limits)
        if checkpointer is None:
            cp_conn = sqlite3.connect(
                settings.data_path("checkpoints.db"), check_same_thread=False
            )
            checkpointer = SqliteSaver(cp_conn)
        # Spec 36, B2.6 #5: a failed checkpoint write does not stop the turn.
        self.saver = GuardedSaver(checkpointer)
        checkpointer = self.saver
        if services.tools is None:
            services.tools = build_tools(settings, services.clock)
        self.graph = build_graph(
            checkpointer,
            flows={"report": build_report_flow(), "delete": build_delete_flow()},
        )
        self.monotonic: Callable[[], float] = (
            time.monotonic
        )  # a fake clock in the tests
        self.guard = SqlGuard(user, services.schema, services.values)
        self.empty_checker = EmptyResultChecker(
            self.guard, services.bq, services.values
        )
        self.conversation_id = conversation_id or uuid.uuid4().hex
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO conversations (id, user_id, started_at) "
                "VALUES (?, ?, ?)",
                (self.conversation_id, user.id, datetime.now(UTC).isoformat()),
            )
        if conversation_id and self.pending:
            # Spec 33, B2.6 #16: a delete that waited in an earlier session expires at load.
            self.cancel_pending("expired")

    # --- turns --------------------------------------------------------------------------------
    @property
    def _config(self) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": self.conversation_id},
            "recursion_limit": self.config.settings.resilience.graph_recursion_limit,
        }

    @property
    def pending(self) -> ConfirmationView | None:
        """The delete that waits for the typed confirmation, or None."""
        for item in self.graph.get_state(self._config).interrupts:
            value = item.value
            if isinstance(value, dict) and value.get("type") == "confirm_delete":
                return ConfirmationView(
                    kind="delete_reports",
                    rows=tuple(dict(r) for r in value.get("rows") or []),
                    count=int(value.get("count") or 0),
                    expected_text=str(value.get("expected_text") or ""),
                    text=str(value.get("text") or ""),
                    not_found=tuple(value.get("not_found") or ()),
                )
        return None

    def send(self, text: str) -> TurnResult:
        """The next user input: the answer to a waiting delete, or a new question."""
        if self.pending is not None:
            return self._run(Command(resume=text), resume=True)
        return self._run(
            {
                "messages": [{"role": "user", "text": text}],
                "user": self.user.as_state(),
                "conversation_id": self.conversation_id,
                "turn": {},
            },
            resume=False,
        )

    def ask(self, question: str) -> TurnResult:
        """The same as `send()` (the name of spec 31, used by the evaluation runner)."""
        return self.send(question)

    def cancel_pending(self, reason: str = "abandoned") -> TurnResult | None:
        """Cancel a waiting delete with the reason `abandoned` or `expired`. Nothing is deleted."""
        if self.pending is None:
            return None
        return self._run(Command(resume={"cancel": reason}), resume=True)

    def close(self) -> TurnResult | None:
        """End the chat. A waiting delete is cancelled (spec 33, B2.6 #15)."""
        return self.cancel_pending("abandoned")

    def _run(self, graph_input: Any, resume: bool) -> TurnResult:
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
        self.saver.write_failed = False
        with span(
            "turn",
            "turn",
            user_id=self.user.id,
            conversation_id=self.conversation_id,
            resume=resume,
        ) as sp:
            trace_id = current_trace_id()
            try:
                state = self.graph.invoke(graph_input, self._config, context=context)
            except KeyboardInterrupt:
                # Spec 36, B2.6 #4: Ctrl-C stops the turn and the running query.
                cancel = getattr(self.services.bq, "cancel_running", None)
                if callable(cancel):
                    cancel()
                sp.set_attribute("status", "cancelled")
                return self._stopped(
                    budget, trace_id, "cancelled", messages.text("stopped")
                )
            except Exception:  # noqa: BLE001 - the chat continues after any bug (spec 36)
                log_exception("turn", trace_id)
                sp.set_attribute("error_type", ErrorType.INTERNAL_ERROR.value)
                sp.set_attribute("status", "error")
                return self._stopped(
                    budget,
                    trace_id,
                    "error",
                    internal_message(trace_id),
                    ErrorType.INTERNAL_ERROR.value,
                )
            turn = state.get("turn") or {}
            totals = budget.totals()
            for key, value in totals.items():
                sp.set_attribute(key, value)
            if turn.get("error_type"):
                sp.set_attribute("error_type", turn["error_type"])
            if turn.get("intent"):
                sp.set_attribute("intent", turn["intent"])
            saved = not self.saver.write_failed
            if not saved:
                sp.set_attribute("checkpoint_failed", True)
        confirmation = self.pending if state.get("__interrupt__") else None
        report = turn.get("report") or {}
        delete = turn.get("delete") or {}
        answer = confirmation.text if confirmation else _answer_of(state, turn)
        if not saved:
            answer = f"{answer}\n\n_{messages.text('not_saved')}_"
        error_type = None if confirmation else turn.get("error_type")
        return TurnResult(
            answer=answer,
            error_type=error_type,
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
                (report.get("built") or {}).get("calculated_figures") or 0
            ),
            report_id=report.get("saved_id"),
            report_files=tuple(report.get("files") or ()),
            confirmation=confirmation,
            replay_input=bool(not confirmation and turn.get("replay_input")),
            delete_outcome=None
            if confirmation
            else delete.get("reason") or delete.get("outcome"),
            batch_id=None if confirmation else delete.get("batch_id"),
            status="error" if error_type else "ok",
            saved=saved,
        )

    def _stopped(
        self,
        budget: TurnBudget,
        trace_id: str | None,
        status: str,
        text: str,
        error_type: str | None = None,
    ) -> TurnResult:
        """The result of a turn that did not end in the graph: Ctrl-C or an unexpected error."""
        return TurnResult(
            answer=text,
            error_type=error_type,
            trace_id=trace_id,
            totals=budget.totals(),
            status=status,
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

    def undo(self, batch_id: str | None = None) -> CommandOutcome:
        """`/undo [batch]`: cancel a waiting delete (`abandoned`), then restore the last batch."""
        lines = []
        if self.cancel_pending("abandoned") is not None:
            lines.append(self.services.prompts.reply("delete.cancelled"))
        ctx = TurnContext(
            services=self.services,
            budget=None,  # type: ignore[arg-type] - an undo makes no model call and no query
            user=self.user,
            guard=self.guard,
            empty_checker=self.empty_checker,
            store=self.conn,
        )
        result = undo_batch(
            self.conn,
            self.config.settings,
            self.user,
            batch_id,
            now=self.services.clock(),
            conversation_id=self.conversation_id,
        )
        lines.append(undo_text(ctx, result))
        kind = "message" if result.outcome == "restored" else "error"
        return CommandOutcome(kind, " ".join(lines))

    def command(self, name: str, args: list[str]) -> CommandOutcome:
        """`/reports`, `/open <id>`, `/export <id> --format html|md` (spec 32) and
        `/undo [batch]` (spec 33). A waiting delete stays, except for `/undo`."""
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
            if name == "undo":
                if len(args) > 1:
                    return CommandOutcome("error", "Usage: /undo [batch]")
                return self.undo(args[0] if args else None)
        except ReportNotFound as exc:
            return CommandOutcome("error", exc.user_message)
        except AgentError as exc:
            return CommandOutcome("error", exc.user_message)
        return CommandOutcome("error", f"Unknown command '/{name}'. Type /help.")


def _answer_of(state: dict[str, Any], turn: dict[str, Any]) -> str:
    """The answer of the turn: the last assistant message. When `finish` failed, the turn has no
    new message, and the answer is the internal message that the node wrapper set."""
    last = (state.get("messages") or [{}])[-1]
    if last.get("role") == "assistant" and turn.get("error_type") != "internal_error":
        return str(last.get("text") or "")
    return str(turn.get("answer") or last.get("text") or "")


def build_tools(settings: Settings, clock: Callable[[], datetime]) -> ToolRegistry:
    """The tool registry: `delete_reports` runs only with a confirmation grant (spec 33)."""
    registry = ToolRegistry()
    registry.register(DeleteReportsTool(settings, clock))
    return registry


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
