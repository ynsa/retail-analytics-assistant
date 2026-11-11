"""LangGraph state and per-turn context (spec 00, B3).

The state goes into the checkpoint (SQLite), so it holds only plain data.
Objects that must not be stored (the budget, the clients) go in `TurnContext`.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, TypedDict

from opsfleet_agent.guard.empty_result import EmptyResultChecker
from opsfleet_agent.guard.masker import ResultMasker
from opsfleet_agent.guard.scanner import OutputScanner
from opsfleet_agent.guard.sql_guard import SqlGuard
from opsfleet_agent.guard.value_cache import ValueCache
from opsfleet_agent.identity import UserContext
from opsfleet_agent.limits import TurnBudget
from opsfleet_agent.schema import Schema


class Message(TypedDict):
    role: Literal["user", "assistant"]
    text: str


class TurnState(TypedDict, total=False):
    question: str
    trace_id: str
    sql: str  # the SQL of the model (the guard's scoped SQL is not stored)
    explanation: str
    guard_outcome: str  # spec 30: pass, policy_block or parse_error
    reason_code: str
    model_feedback: (
        str  # spec 30: for the retry of spec 31 (parse error or empty-result error)
    )
    result_table: str  # the masked rows as a data block for the answer prompt
    row_count: int
    truncated: bool
    notes: str  # spec 30: the note of the empty-result checks for the answer prompt
    scanner_hits: int  # spec 30: text that the output scanner removed
    error_type: str
    answer: str


class AgentState(TypedDict, total=False):
    messages: Annotated[list[Message], operator.add]
    user: dict[str, Any]  # UserContext as plain data, set by the CLI
    conversation_id: str
    turn: TurnState
    pending_confirmation: dict[str, Any] | None  # spec 33
    summary: str  # spec 34


@dataclass
class Services:
    """Long-lived objects that the nodes use. Built once for each CLI run."""

    settings: Any  # config.Settings
    llm: Any  # llm.GeminiClient
    bq: Any  # bq.BigQueryService
    schema: Schema
    values: ValueCache  # spec 30: filter values, date range and brand list
    prompts: dict[str, str]
    masker: ResultMasker = field(default_factory=ResultMasker)
    scanner: OutputScanner = field(default_factory=OutputScanner)


@dataclass
class TurnContext:
    """The context of one user turn. LangGraph passes it to each node, but does not store it."""

    services: Services
    budget: TurnBudget
    user: UserContext
    guard: SqlGuard  # spec 30: built once for each session, with the user's scope
    empty_checker: EmptyResultChecker
    # Objects of this turn that the checkpoint must not store, for example the GuardResult with
    # its query parameters. A node that does not find an entry builds it again from the state.
    scratch: dict[str, Any] = field(default_factory=dict)
