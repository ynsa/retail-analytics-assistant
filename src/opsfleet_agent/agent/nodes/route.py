"""`route`: one small structured model call that classifies the message (spec 31, B2.2).

The route call never sees query results, so data cannot change the route. If the JSON is not
valid after a second attempt, the intent is `analysis`: the guard still protects the data.

The context has the pinned facts and the last 2 turns (spec 34, B2.1), not the summary. The format
request and the filters and terms that the user sets for the conversation go into the pinned facts
at once, so the next nodes of the same turn use them.
"""

from __future__ import annotations

from typing import Any, Literal

from langgraph.runtime import Runtime
from pydantic import BaseModel, Field, ValidationError

from opsfleet_agent.agent.nodes.common import is_manager, node, turn_of
from opsfleet_agent.agent.state import AgentState, TurnContext
from opsfleet_agent.errors import AgentError
from opsfleet_agent.llm import SCHEMA_MISMATCH
from opsfleet_agent.memory.context_builder import builder_of
from opsfleet_agent.memory.pinned import with_route_facts

ROUTE_ATTEMPTS = 2
NO_FACT_INTENTS = frozenset({"unsafe", "off_topic"})


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


class StandingFilter(BaseModel):
    """A filter that the user sets for the rest of the conversation (spec 34, B7)."""

    field: str
    value: str | None = None  # None removes the filter on this field


class TermDefinition(BaseModel):
    """A term that the user defines for the conversation, for example "big order"."""

    term: str
    meaning: str


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
    schema_kind: Literal["access", "data", "conversation"] | None = None
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
    # Spec 34: facts that the user sets for the conversation. Code pins them.
    set_filters: list[StandingFilter] = Field(default_factory=list)
    clear_filters: bool = False
    set_definitions: list[TermDefinition] = Field(default_factory=list)


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
    request = builder_of(ctx.services).build(
        "route", system=system, state=state, user_message=turn["question"]
    )
    decision = FALLBACK
    for _ in range(ROUTE_ATTEMPTS):
        try:
            result = ctx.services.llm.generate(
                ctx.budget,
                system=request.system,
                prompt=request.prompt,
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
    # Spec 34: the format and the facts that the user set apply from this turn on. An unsafe or
    # off-topic message sets no fact.
    facts = route_data if decision.intent not in NO_FACT_INTENTS else {}
    return {
        "turn": {**turn, "intent": decision.intent, "route": route_data},
        "pinned": with_route_facts(state.get("pinned"), facts),
    }


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
