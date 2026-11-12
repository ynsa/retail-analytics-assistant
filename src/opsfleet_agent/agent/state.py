"""LangGraph state and per-turn context (spec 00, B3, extended by spec 31, B3).

The state goes into the checkpoint (SQLite), so it holds only plain data.
Objects that must not be stored (the budget, the clients, raw rows) go in `TurnContext`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, TypedDict

from opsfleet_agent.agent.context import PromptLibrary
from opsfleet_agent.agent.formatting import ResultData
from opsfleet_agent.guard.empty_result import EmptyResultChecker
from opsfleet_agent.guard.masker import ResultMasker
from opsfleet_agent.guard.scanner import OutputScanner
from opsfleet_agent.guard.sql_guard import SqlGuard
from opsfleet_agent.guard.value_cache import ValueCache
from opsfleet_agent.identity import UserContext
from opsfleet_agent.limits import TurnBudget
from opsfleet_agent.memory.messages import Message, messages_reducer
from opsfleet_agent.schema import Schema

Intent = Literal[
    "analysis",
    "schema",
    "clarify",
    "report",
    "delete",
    "chitchat",
    "off_topic",
    "unsafe",
]
LoopOutcome = Literal["answer", "give_up", "policy_block"]


def utc_now() -> datetime:
    return datetime.now(UTC)


class StepRecord(TypedDict, total=False):
    """One query step of a question (spec 31, B3)."""

    purpose: str
    sql: str  # the SQL of the model (the guard's scoped SQL is not stored)
    guard_outcome: str
    reason_code: str
    result_ref: str
    row_count: int
    error_type: str
    attempts: int
    brands: list[str]  # brand literals of the guard filters, for the footer
    entities: dict[
        str, list[str]
    ]  # spec 32: filter literals on brand, category, state, ...
    job_id: str  # spec 32: the BigQuery job of the step, for the report appendix
    bytes_billed: int


class RetryInfo(TypedDict):
    """The feedback of a failed attempt for the next `write_sql` call (spec 31, B2.4)."""

    sql: str
    error_type: str
    message: str


class TurnState(TypedDict, total=False):
    question: str
    trace_id: str
    # route (B2.2)
    intent: str
    route: dict[str, Any]
    # data loop (B2.3, B2.4)
    steps: list[StepRecord]
    step_index: int  # the current step, 0 to max_steps - 1
    attempt: int  # the SQL attempts of the current step
    plan: dict[str, Any]  # the write_sql output of the current attempt
    plans: list[dict[str, Any]]  # the write_sql output of each finished step
    sql: str
    next: str  # the next node inside the data loop
    loop_outcome: str  # LoopOutcome, set when the data loop ends normally
    retry: RetryInfo | None
    guard_outcome: str  # spec 30: pass, policy_block or parse_error
    reason_code: str
    model_feedback: (
        str  # spec 30: the feedback of a parse error or an empty-result error
    )
    block_message: str  # the guard message of a policy block
    results: list[ResultData]  # masked and formatted results of this turn (r1, r2, ...)
    row_count: int
    notes: list[str]  # notes for the answer prompt (empty-result checks, missing steps)
    cannot_answer: str
    # answer (B2.5)
    error_type: str
    answer: str
    footer: str
    calc_errors: int
    unverified_figures: int
    scanner_hits: int
    prompt_leak: bool
    final_text: str
    # reports (spec 32) and the delete flow (spec 33)
    single_step: bool  # a report query: the data loop runs one step only
    report: dict[
        str, Any
    ]  # the report flow data: plan, queries, draft, report ID, files
    delete: dict[str, Any]  # the delete flow data: scope, matches, outcome, batch ID
    confirmation_prompt: str  # the list text that the confirmation showed
    confirm_text: str  # the user's typed answer to the confirmation
    replay_input: bool  # spec 33, A2.8: the answer was a new message, send it again


class AgentState(TypedDict, total=False):
    # Spec 34: only the recent window (4 to 8 turns). The reducer appends a list and applies
    # `compact(n)`. The full log is in `conversation_turns`.
    messages: Annotated[list[Message], messages_reducer]
    user: dict[str, Any]  # UserContext as plain data, set by the CLI
    conversation_id: str
    turn: TurnState
    pinned: dict[str, Any]  # spec 34, B2.2: the pinned facts that code writes
    pending_confirmation: dict[str, Any] | None  # spec 33, B2.3
    summary: str  # spec 34, B2.4: the rolling summary of the older turns
    turn_no: int  # the number of the current turn (spec 33, B2.3 `created_turn`)


@dataclass
class Services:
    """Long-lived objects that the nodes use. Built once for each CLI run."""

    settings: Any  # config.Settings
    llm: Any  # llm.GeminiClient
    bq: Any  # bq.BigQueryService
    schema: Schema
    values: ValueCache  # spec 30: filter values, date range and brand list
    prompts: PromptLibrary
    masker: ResultMasker = field(default_factory=ResultMasker)
    scanner: OutputScanner = field(default_factory=OutputScanner)
    clock: Callable[[], datetime] = field(default=utc_now)  # a fake clock in the tests
    tools: Any | None = None  # agent.tools.ToolRegistry (spec 33), set by the session


@dataclass
class TurnContext:
    """The context of one user turn. LangGraph passes it to each node, but does not store it."""

    services: Services
    budget: TurnBudget
    user: UserContext
    guard: SqlGuard  # spec 30: built once for each session, with the user's scope
    empty_checker: EmptyResultChecker
    store: Any | None = (
        None  # sqlite3.Connection of data/app.db (reports, audit events)
    )
    # Objects of this turn that the checkpoint must not store: the GuardResult with its query
    # parameters, the raw query result before the masker, the persona text of this turn, and the
    # scoped SQL that ran. A node that does not find an entry builds it again from the state.
    scratch: dict[str, Any] = field(default_factory=dict)
