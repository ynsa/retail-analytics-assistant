"""Helpers for all nodes: the node wrapper, the turn state, and the scope texts (spec 31).

Each node runs in a `node` span. An `AgentError` (also `LimitExceeded`) ends the turn with its
one-sentence message, and the chat continues. The graph catches it in the node wrapper, not in the
CLI (spec 31, B2.1).
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any

from langgraph.runtime import Runtime

from opsfleet_agent.agent.state import AgentState, TurnContext, TurnState
from opsfleet_agent.config import Role
from opsfleet_agent.errors import AgentError, ErrorType
from opsfleet_agent.identity import UserContext
from opsfleet_agent.tracing import span

INTERNAL_MESSAGE = (
    "Something went wrong on my side, so I could not answer. Please try again."
)
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
            except AgentError as exc:
                return end_turn(turn_of(state), exc.user_message, exc.error_type.value)
            except Exception:  # noqa: BLE001 - the chat must continue after any bug
                return end_turn(
                    turn_of(state), INTERNAL_MESSAGE, ErrorType.INTERNAL_ERROR.value
                )

        return wrapper

    return decorate


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


def history_text(state: AgentState, turns: int) -> str:
    """The last turns before the current question, as a record (a `<history>` block)."""
    messages = (state.get("messages") or [])[:-1]
    recent = messages[-(turns * 2) :] if turns else []
    if not recent:
        return "(none)"
    return "\n".join(f"{m['role']}: {m['text'][:600]}" for m in recent)


def pinned_text(pinned: dict[str, Any] | None) -> str:
    """The pinned facts as short YAML-like lines (spec 34 extends them)."""
    if not pinned:
        return "(none)"
    lines = []
    for key in ("format", "active_filters", "definitions_in_use"):
        value = pinned.get(key)
        if value:
            lines.append(f"{key}: {value}")
    return "\n".join(lines) or "(none)"


def user_format(state: AgentState, turn: TurnState) -> str | None:
    """The format of this turn's request, else the pinned format (the latest wins)."""
    route = turn.get("route") or {}
    return route.get("format_request") or (state.get("pinned") or {}).get("format")
