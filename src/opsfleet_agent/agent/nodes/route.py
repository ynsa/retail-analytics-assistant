"""`route`: one small structured model call that classifies the message (spec 31, B2.2).

The route call never sees query results, so data cannot change the route. If the JSON is not
valid after a second attempt, the intent is `analysis`: the guard still protects the data.
"""

from __future__ import annotations

from typing import Any, Literal

from langgraph.runtime import Runtime
from pydantic import BaseModel, Field, ValidationError

from opsfleet_agent.agent.nodes.common import (
    history_text,
    is_manager,
    node,
    pinned_text,
    turn_of,
)
from opsfleet_agent.agent.state import AgentState, TurnContext
from opsfleet_agent.errors import AgentError
from opsfleet_agent.llm import SCHEMA_MISMATCH

ROUTE_HISTORY_TURNS = 2
ROUTE_ATTEMPTS = 2


class ReportRequest(BaseModel):
    action: Literal["create", "save_last", "list", "open", "export"] | None = None
    topic: str | None = None
    period: str | None = None
    file_format: Literal["html", "md"] | None = None
    report_id: str | None = None


class DeleteRequest(BaseModel):
    scope: Literal["conversation", "entity", "ids", "all", "undo"] | None = None
    entity: str | None = None
    ids: list[str] = Field(default_factory=list)


class RouteDecision(BaseModel):
    intent: Literal[
        "analysis",
        "schema",
        "clarify",
        "report",
        "delete",
        "chitchat",
        "off_topic",
        "unsafe",
    ]
    follow_up: bool = False
    format_request: Literal["table", "bullets", "short", "detailed"] | None = None
    schema_kind: Literal["access", "data"] | None = None
    chitchat_kind: (
        Literal["greeting", "thanks", "acknowledgement", "format_only"] | None
    ) = None
    unsafe_kind: (
        Literal["prompt_request", "identity_change", "rule_change", "other"] | None
    ) = None
    clarify_question: str | None = None
    clarify_options: list[str] = Field(default_factory=list)
    report_request: ReportRequest | None = None
    delete_request: DeleteRequest | None = None


FALLBACK = RouteDecision(intent="analysis")


def _is_format_error(exc: Exception) -> bool:
    if isinstance(exc, ValidationError):
        return True
    return isinstance(exc, AgentError) and exc.detail == SCHEMA_MISMATCH


@node("route")
def route(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    ctx = runtime.context
    lib = ctx.services.prompts
    turn = turn_of(state)
    user = ctx.user
    system = lib.system(
        "route",
        role=user.role.value,
        brands=", ".join(user.brands) if is_manager(user) else "all brands",
    )
    contents = (
        f"<pinned>\n{pinned_text(state.get('pinned'))}\n</pinned>\n\n"
        f"<history>\n{history_text(state, ROUTE_HISTORY_TURNS)}\n</history>\n\n"
        f"<user_message>\n{turn['question']}\n</user_message>"
    )
    decision = FALLBACK
    for _ in range(ROUTE_ATTEMPTS):
        try:
            result = ctx.services.llm.generate(
                ctx.budget,
                system=system,
                prompt=contents,
                schema=RouteDecision,
                purpose="route",
                prompt_version=lib.version("safety", "route"),
            )
        except (AgentError, ValidationError) as exc:
            if _is_format_error(exc):
                continue
            raise
        decision = result.parsed
        break
    route_data = decision.model_dump(exclude_none=True)
    return {"turn": {**turn, "intent": decision.intent, "route": route_data}}


def next_after_route(state: AgentState) -> str:
    turn = turn_of(state)
    if turn.get("answer"):
        return "finish"
    intent = turn.get("intent") or "analysis"
    if intent in ("chitchat", "off_topic", "unsafe", "clarify"):
        return "prepared_reply"
    if intent == "schema":
        return "schema_answer"
    if intent in ("report", "delete"):
        return intent
    return "analysis"
