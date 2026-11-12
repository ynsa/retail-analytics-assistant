"""Helpers for all nodes: the node wrapper, the turn state, and the scope texts (spec 31).

Each node runs in a `node` span. An `AgentError` (also `LimitExceeded`) ends the turn with its
one-sentence message, and the chat continues. The graph catches it in the node wrapper, not in the
CLI (spec 31, B2.1). A failure message ends with "Trace: <8 characters>.", and an unexpected
exception writes its stack trace to `data/logs/agent.log` (spec 36, B2.6).
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any

from langgraph.errors import GraphBubbleUp
from langgraph.runtime import Runtime
from opentelemetry import trace

from opsfleet_agent.agent.state import AgentState, TurnContext, TurnState
from opsfleet_agent.config import Role
from opsfleet_agent.errors import AgentError, ErrorType
from opsfleet_agent.identity import UserContext
from opsfleet_agent.resilience import messages
from opsfleet_agent.resilience.errors_map import bq_down
from opsfleet_agent.resilience.safe_node import (
    failure_answer,
    internal_message,
    log_exception,
)
from opsfleet_agent.tracing import current_trace_id, span

INTERNAL_MESSAGE = messages.text("internal")  # spec 36, A2.11
NodeFn = Callable[[AgentState, Runtime[TurnContext]], dict[str, Any]]
SPAN_FIELDS = ("intent", "step_index", "attempt", "error_type")


def turn_of(state: AgentState) -> TurnState:
    return dict(state.get("turn") or {})  # type: ignore[return-value]


def end_turn(
    turn: TurnState, answer: str, error_type: str | None = None
) -> dict[str, Any]:
    """Finish the turn with a final text. The next node is `scan_output`."""
    out: TurnState = {**turn, "answer": answer}
    if error_type:
        out["error_type"] = error_type
    return {"turn": out}


def node(name: str) -> Callable[[NodeFn], NodeFn]:
    """Run a node in a span. Turn an error into a final answer, so the chat does not stop."""

    def decorate(fn: NodeFn) -> NodeFn:
        @wraps(fn)
        def wrapper(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
            try:
                with span(f"node.{name}", "node") as sp:
                    update = fn(state, runtime)
                    turn = update.get("turn") or {}
                    for key in SPAN_FIELDS:
                        if turn.get(key) is not None:
                            sp.set_attribute(key, turn[key])
                    return update
            except GraphBubbleUp:
                raise  # an interrupt() of the delete confirmation (spec 33) is not an error
            except AgentError as exc:
                turn = turn_of(state)
                trace_id = turn.get("trace_id") or current_trace_id()
                return end_turn(
                    turn, failure_answer(exc, trace_id), exc.error_type.value
                )
            except Exception:  # noqa: BLE001 - the chat continues after any bug (spec 36)
                turn = turn_of(state)
                trace_id = turn.get("trace_id") or current_trace_id()
                log_exception(f"node {name}", trace_id)
                return end_turn(
                    turn, internal_message(trace_id), ErrorType.INTERNAL_ERROR.value
                )

        return wrapper

    return decorate


def require_bigquery(ctx: TurnContext) -> None:
    """Spec 36, B2.4: when the BigQuery breaker is open, a data turn ends before the model writes
    SQL. This saves the model calls of a query that cannot run."""
    available = getattr(ctx.services.bq, "available", None)
    if callable(available) and not available():
        trace.get_current_span().add_event("circuit_open", {"breaker": "bigquery"})
        raise bq_down("circuit open", circuit_open=True)


def is_manager(user: UserContext) -> bool:
    return user.role is Role.MANAGER


def brands_text(brands: list[str] | tuple[str, ...]) -> str:
    """`Calvin Klein`, `Calvin Klein and Carhartt`, `A, B and C`."""
    names = list(brands)
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def scope_text(user: UserContext) -> str:
    if not is_manager(user):
        return "all brands and all customers (CEO)"
    return (
        f"only the brands {', '.join(user.brands)} (manager). Customer figures cover only "
        "the customers who bought these brands"
    )


def user_format(state: AgentState, turn: TurnState) -> str | None:
    """The format of this turn's request, else the pinned format (the latest wins)."""
    route = turn.get("route") or {}
    return route.get("format_request") or (state.get("pinned") or {}).get("format")
