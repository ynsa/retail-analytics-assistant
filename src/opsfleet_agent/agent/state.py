"""LangGraph state and per-turn context (spec 00, B3).

The state goes into the checkpoint (SQLite), so it holds only plain data.
Objects that must not be stored (the budget, the clients) go in `TurnContext`.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict

from opsfleet_agent.identity import UserContext
from opsfleet_agent.limits import TurnBudget


class Message(TypedDict):
    role: Literal["user", "assistant"]
    text: str


class TurnState(TypedDict, total=False):
    question: str
    trace_id: str
    sql: str
    explanation: str
    result_table: str  # small Markdown table of the masked rows for the answer prompt
    row_count: int
    truncated: bool
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
    schema_text: str
    prompts: dict[str, str]


@dataclass
class TurnContext:
    """The context of one user turn. LangGraph passes it to each node, but does not store it."""

    services: Services
    budget: TurnBudget
    user: UserContext
