"""AssistantSession: one conversation of one user (spec 37). The CLI and the UI only display.

The session builds the services once, creates a TurnBudget for each turn, runs the graph in a
`turn` span, and records the conversation in the database.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from opsfleet_agent import capabilities
from opsfleet_agent.agent.context import PROTECTED_PROMPTS, PromptLibrary
from opsfleet_agent.agent.graph import build_graph
from opsfleet_agent.agent.state import Services, TurnContext
from opsfleet_agent.agent.tools import ToolRegistry
from opsfleet_agent.bq import BigQueryService
from opsfleet_agent.commands import CommandOutcome, run_command
from opsfleet_agent.config import AppConfig, Settings
from opsfleet_agent.errors import ConfigError, ErrorType
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
from opsfleet_agent.memory import conversations
from opsfleet_agent.memory.conversations import canonical_id, new_conversation_id
from opsfleet_agent.memory.pinned import with_user
from opsfleet_agent.observability.progress import PROGRESS, ProgressEvent
from opsfleet_agent.observability.turn_metrics import (
    TURN_SPANS,
    TurnFacts,
    turn_metrics,
    write_turn_record,
)
from opsfleet_agent.reports.delete_flow import build_delete_flow, undo_text
from opsfleet_agent.reports.deletion import DeleteReportsTool, undo_batch
from opsfleet_agent.reports.flow import build_report_flow
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

_log = logging.getLogger("opsfleet_agent.session")


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
    turn_no: int | None = None  # spec 34: the number of the turn in the conversation


@dataclass(frozen=True)
class ResumeView:
    """What the CLI shows at `--resume` (spec 34, A2.6 and B2.5)."""

    conversation_id: str
    turns: tuple[Any, ...]  # memory.conversations.TurnRow: the last 3 turns
    summary: tuple[str, ...]  # the first 3 lines of the summary
    notes: tuple[str, ...] = ()  # pinned notes, for example "Your brands changed …"


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
class Greeting:
    """The greeting panel of the CLI (spec 37, A2.1)."""

    name: str
    role: str
    brands: tuple[str, ...]  # empty: all brands
    data_from: date | None
    data_to: date
    conversation_id: str
    resumed: bool = False


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
    cache (spec 30) costs one small query (about 40 MB billed) when it is missing or from an
    earlier UTC day. The query counts in the daily limit."""
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
            capture_prompts=settings.tracing.capture_prompts,
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
        init_tracing(
            settings.data_path("traces"),
            settings.tracing.attribute_max_chars,
            settings.tracing.otlp_endpoint,
        )
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
        self.conversation_id = (
            canonical_id(conversation_id) if conversation_id else new_conversation_id()
        )
        self.last_trace_id: str | None = None  # for `/trace` without an ID
        self.prompt_version = services.prompts.version(*PROTECTED_PROMPTS)
        conversations.create(self.conn, self.conversation_id, user.id, services.clock())
        if conversation_id and self.pending:
            # Spec 33, B2.6 #16: a delete that waited in an earlier session expires at load.
            self.cancel_pending("expired")

    @classmethod
    def resume(
        cls,
        config: AppConfig,
        user: UserContext,
        services: Services,
        conversation_id: str,
        conn: sqlite3.Connection | None = None,
        checkpointer: Any | None = None,
    ) -> AssistantSession:
        """`chat --resume <id>` (spec 34, B2.5). The conversation must belong to the user and be
        active within 90 days. Raises `ConversationNotFound` or `ConversationTooOld`.

        A waiting delete expires. The pinned user comes from the current `users.yaml`: when the
        brands changed, the pinned facts get the note "Your brands changed since the last
        session"."""
        settings = config.settings
        own_conn = conn or connect(
            settings.data_path("app.db"),
            busy_timeout_s=settings.resilience.sqlite_busy_timeout_s,
        )
        row = conversations.load_for_resume(
            own_conn,
            conversation_id,
            user.id,
            services.clock(),
            settings.context.resume_max_days,
        )
        session = cls(
            config,
            user,
            services,
            conn=own_conn,
            checkpointer=checkpointer,
            conversation_id=row.id,
        )
        session._refresh_pinned_user()
        return session

    def _refresh_pinned_user(self) -> None:
        snapshot = self.graph.get_state(self._config)
        if not snapshot.values:
            return
        pinned = with_user(
            snapshot.values.get("pinned"),
            self.user,
            self.services.values.first_date("order_items"),
            self.services.clock().date(),
        )
        self.graph.update_state(self._config, {"pinned": pinned}, as_node="summarize")

    def resume_view(self, turns: int = 3, summary_lines: int = 3) -> ResumeView:
        """What the CLI shows at resume: the last turns and the first lines of the summary."""
        values = self.graph.get_state(self._config).values or {}
        summary = [
            line for line in (values.get("summary") or "").splitlines() if line.strip()
        ]
        pinned = values.get("pinned") or {}
        return ResumeView(
            conversation_id=self.conversation_id,
            turns=tuple(
                conversations.last_turns(self.conn, self.conversation_id, turns)
            ),
            summary=tuple(summary[:summary_lines]),
            notes=tuple(pinned.get("notes") or ()),
        )

    def turn_count(self) -> int:
        row = self.conn.execute(
            "SELECT turn_count FROM conversations WHERE id = ?", (self.conversation_id,)
        ).fetchone()
        return int(row["turn_count"] or 0) if row else 0

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

    def send(
        self,
        text: str,
        on_answer: Callable[[TurnResult], None] | None = None,
        on_progress: Callable[[ProgressEvent], None] | None = None,
    ) -> TurnResult:
        """The next user input: the answer to a waiting delete, or a new question.

        `on_answer` (spec 34, B2.4) gets the result as soon as the answer is ready, before the
        summary of older turns runs. The returned result also has the cost of the summary.
        `on_progress` (spec 37, B2.2) gets an event when a node of the graph starts."""
        if self.pending is not None:
            return self._run(
                Command(resume=text),
                resume=True,
                on_answer=on_answer,
                on_progress=on_progress,
            )
        turn_no = self._turn_no() + 1
        return self._run(
            {
                # `load_context` adds 1. The input also corrects a number that a failed
                # checkpoint write did not keep, so two turns never share one number.
                "turn_no": turn_no - 1,
                "messages": [
                    {
                        "role": "user",
                        "turn_no": turn_no,
                        "kind": "question",
                        "text": text,
                    }
                ],
                "user": self.user.as_state(),
                "conversation_id": self.conversation_id,
                "turn": {},
            },
            resume=False,
            on_answer=on_answer,
            question=text,
            on_progress=on_progress,
        )

    def _turn_no(self) -> int:
        """The number of the last turn: from the checkpoint, or from the conversation log when
        a checkpoint write failed (spec 36, B2.6 #5)."""
        values = self.graph.get_state(self._config).values or {}
        row = self.conn.execute(
            "SELECT MAX(turn_no) AS n FROM conversation_turns WHERE conversation_id = ?",
            (self.conversation_id,),
        ).fetchone()
        logged = int((row["n"] if row else 0) or 0)
        return max(int(values.get("turn_no") or 0), logged)

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

    def _run(
        self,
        graph_input: Any,
        resume: bool,
        on_answer: Callable[[TurnResult], None] | None = None,
        question: str | None = None,
        on_progress: Callable[[ProgressEvent], None] | None = None,
    ) -> TurnResult:
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
        started_at = self.services.clock()
        turn_no = self._turn_no() + (0 if resume else 1)
        self.saver.write_failed = False
        with span(
            "turn",
            "turn",
            user_id=self.user.id,
            conversation_id=self.conversation_id,
            resume=resume,
            model=self.config.settings.models.default,
            prompt_version=self.prompt_version,
        ) as sp:
            trace_id = current_trace_id()
            self.last_trace_id = trace_id
            if on_progress is not None and trace_id:
                PROGRESS.listen(trace_id, on_progress)
            _log.info(
                "turn start",
                extra={
                    "user_id": self.user.id,
                    "conversation_id": self.conversation_id,
                    "resume": resume,
                    "user_text": question or "",
                },
            )
            try:
                state = self._stream(graph_input, context, trace_id, on_answer)
            except KeyboardInterrupt:
                # Spec 36, B2.6 #4: Ctrl-C stops the turn and the running query.
                cancel = getattr(self.services.bq, "cancel_running", None)
                if callable(cancel):
                    cancel()
                sp.set_attribute("status", "cancelled")
                result = self._stopped(
                    budget, trace_id, "cancelled", messages.text("stopped")
                )
            except Exception:  # noqa: BLE001 - the chat continues after any bug (spec 36)
                log_exception("turn", trace_id)
                sp.set_attribute("error_type", ErrorType.INTERNAL_ERROR.value)
                sp.set_attribute("status", "error")
                result = self._stopped(
                    budget,
                    trace_id,
                    "error",
                    internal_message(trace_id),
                    ErrorType.INTERNAL_ERROR.value,
                )
            else:
                result = self._result(state, budget, context, trace_id)
                turn = state.get("turn") or {}
                for key, value in result.totals.items():
                    sp.set_attribute(key, value)
                if turn.get("error_type"):
                    sp.set_attribute("error_type", turn["error_type"])
                if turn.get("intent"):
                    sp.set_attribute("intent", turn["intent"])
                if not result.saved:
                    sp.set_attribute("checkpoint_failed", True)
                turn_no = int(state.get("turn_no") or turn_no)
            finally:
                PROGRESS.stop(trace_id)
            sp.set_attribute("turn_no", turn_no)
            sp.set_attribute("status", result.status)
            _log.info(
                "turn end",
                extra={
                    "user_id": self.user.id,
                    "conversation_id": self.conversation_id,
                    "turn_no": turn_no,
                    "status": result.status,
                    "error_type": result.error_type,
                    "seconds": result.totals.get("seconds"),
                },
            )
        self._record(result, turn_no, started_at, question, resume)
        return result

    def _stream(
        self,
        graph_input: Any,
        context: TurnContext,
        trace_id: str | None,
        on_answer: Callable[[TurnResult], None] | None,
    ) -> dict[str, Any]:
        """Run the graph. When `finish` ends, `on_answer` gets the answer (spec 34, B2.4).

        The checkpoint of each step is written before the next step (`durability="sync"`), so
        the note "This turn is not saved." is correct already in `on_answer`. A Ctrl-C or an error
        after `finish` (in the summary) keeps the answer: the summary runs again at the next turn.
        """
        state: dict[str, Any] = {}
        finished = answered = False
        try:
            for mode, chunk in self.graph.stream(
                graph_input,
                self._config,
                context=context,
                stream_mode=["updates", "values"],
                durability="sync",
            ):
                if mode == "updates":
                    finished = finished or "finish" in chunk
                    continue
                state = chunk
                if finished and not answered and on_answer is not None:
                    answered = True
                    on_answer(self._result(state, context.budget, context, trace_id))
        except (Exception, KeyboardInterrupt) as exc:
            if not finished:
                raise
            if isinstance(exc, Exception):
                log_exception("after the answer", trace_id)
            cancel = getattr(self.services.bq, "cancel_running", None)
            if callable(cancel):
                cancel()
        return state

    def _result(
        self,
        state: dict[str, Any],
        budget: TurnBudget,
        context: TurnContext,
        trace_id: str | None,
    ) -> TurnResult:
        turn = state.get("turn") or {}
        saved = not self.saver.write_failed
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
            totals=budget.totals(),
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
            turn_no=int(state.get("turn_no") or 0) or None,
        )

    def _record(
        self,
        result: TurnResult,
        turn_no: int,
        started_at: datetime,
        question: str | None,
        resume: bool = False,
    ) -> None:
        """Spec 34, B2.5 and spec 35, B2.4: the turn row in `conversation_turns`, with the
        figures of the turn. A failed write of the log does not stop the chat."""
        spans = TURN_SPANS.take(result.trace_id)
        if turn_no <= 0:
            return
        try:
            conversations.record_turn(
                self.conn,
                self.services.scanner,
                conversation_id=self.conversation_id,
                turn_no=turn_no,
                trace_id=result.trace_id,
                started_at=started_at,
                user_text=question or "",
                answer_text=result.answer,
                intent=result.intent,
                status=result.status,
                error_type=result.error_type,
                user_id=self.user.id,
            )
            write_turn_record(
                self.conn,
                self.conversation_id,
                turn_no,
                turn_metrics(_facts(result), spans, self.user.id),
                resumed=resume,
            )
        except sqlite3.Error:
            log_exception("conversation log", result.trace_id)

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
        """A slash command (spec 37, A2.4): no model call and no query. A waiting delete stays,
        except for `/undo`."""
        return run_command(self, name, args)

    def last_trace(self) -> str | None:
        """The trace of the last turn of this chat, or of this conversation (after a resume)."""
        if self.last_trace_id:
            return self.last_trace_id
        row = self.conn.execute(
            "SELECT trace_id FROM conversation_turns WHERE conversation_id = ? "
            "AND trace_id IS NOT NULL ORDER BY turn_no DESC LIMIT 1",
            (self.conversation_id,),
        ).fetchone()
        return row["trace_id"] if row else None

    # --- start (spec 37, B2.6) -------------------------------------------------------------------
    def greeting(self, resumed: bool = False) -> Greeting:
        return Greeting(
            name=self.user.name,
            role=self.user.role.value,
            brands=tuple(self.user.brands),
            data_from=self.services.values.first_date("order_items"),
            data_to=self.services.clock().date(),
            conversation_id=self.conversation_id,
            resumed=resumed,
        )

    def missing_capabilities(self) -> list[str]:
        """The parts of the prototype that are not active (spec 37, B2.5)."""
        return capabilities.missing()


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


def _facts(result: TurnResult) -> TurnFacts:
    return TurnFacts(
        answer=result.answer,
        intent=result.intent,
        status=result.status,
        error_type=result.error_type,
        totals=result.totals,
        steps=result.steps,
        confirmation=result.confirmation is not None,
        delete_outcome=result.delete_outcome,
        report_id=result.report_id,
    )
