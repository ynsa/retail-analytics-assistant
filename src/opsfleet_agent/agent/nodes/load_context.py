"""`load_context`: start a new turn (spec 31, B2.1).

It resets the turn state, reads the persona file for this turn (no restart needed after a change)
and refuses a message over the length limit without a model call.
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime
from opentelemetry import trace

from opsfleet_agent.agent.nodes.common import end_turn, node
from opsfleet_agent.agent.state import AgentState, TurnContext, TurnState
from opsfleet_agent.store.audit import confirm_span, write_audit
from opsfleet_agent.tracing import current_trace_id


def drop_stale_confirmation(state: AgentState, ctx: TurnContext) -> dict[str, Any]:
    """A pending delete in the state at the start of a new turn is old: cancel it (spec 33,
    B2.6 #16). The session normally resumes the paused graph, so this is a second net."""
    pending = state.get("pending_confirmation")
    if not pending:
        return {}
    if ctx.store is not None:
        write_audit(
            ctx.store,
            user_id=ctx.user.id,
            conversation_id=state.get("conversation_id"),
            action="delete_cancelled",
            details={"reason": "expired", "report_ids": pending.get("report_ids", [])},
        )
    confirm_span("cancel", "cancelled", "expired")
    return {"pending_confirmation": None}


@node("load_context")
def load_context(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    ctx = runtime.context
    question = state["messages"][-1]["text"]
    turn: TurnState = {
        "question": question,
        "trace_id": current_trace_id() or "",
        "steps": [],
        "plans": [],
        "results": [],
        "notes": [],
        "step_index": 0,
        "attempt": 0,
        "retry": None,
    }
    persona = ctx.services.prompts.read_persona()
    ctx.scratch["persona"] = persona.text
    sp = trace.get_current_span()
    sp.set_attribute("persona_chars", len(persona.text))
    if persona.cut:  # spec 31, B4 case 14: a warning in the trace
        sp.add_event(
            "persona_cut", {"max_chars": ctx.services.prompts.persona_max_chars}
        )
    extra: dict[str, Any] = {
        "turn_no": int(state.get("turn_no") or 0) + 1,
        **drop_stale_confirmation(state, ctx),
    }
    limit = ctx.services.settings.agent.max_user_message_chars
    if len(question) > limit:
        sp.set_attribute("message_too_long", True)
        reply = ctx.services.prompts.reply("too_long", limit=f"{limit:,}")
        return {**end_turn(turn, reply), **extra}
    return {"turn": turn, **extra}
