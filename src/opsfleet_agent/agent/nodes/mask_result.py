"""`mask_result`: mask and format the rows, then decide the next step (spec 31, B2.1 and B2.5).

The masker of spec 30 cleans the rows. Code stores the values with a column kind, so the answer
prompt gets formatted figures and the calculations use the exact values. Then:

- `final = true`, or the step limit is reached: the loop ends and the answer follows.
- `final = false`: a new step starts with `attempt = 0`.
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from opsfleet_agent.agent.formatting import store_result
from opsfleet_agent.agent.nodes.common import node, turn_of
from opsfleet_agent.agent.nodes.guard_sql import guard_result_for
from opsfleet_agent.agent.nodes.loop_control import with_step
from opsfleet_agent.agent.state import AgentState, TurnContext
from opsfleet_agent.guard.masker import MaskedResult

EMPTY = MaskedResult(columns=[], rows=[], row_count=0, truncated=False)


@node("mask_result")
def mask_result(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    ctx = runtime.context
    services = ctx.services
    turn = turn_of(state)
    raw = ctx.scratch.pop("query_result", None)
    masked = (
        services.masker.mask(raw, guard_result_for(runtime, turn)) if raw else EMPTY
    )
    results = list(turn.get("results") or [])
    plan = turn.get("plan") or {}
    stored = store_result(masked, f"r{len(results) + 1}", plan.get("purpose", ""))
    results.append(stored)
    turn = with_step(turn, result_ref=stored["ref"], row_count=stored["row_count"])
    turn = {
        **turn,
        "results": results,
        "plans": [*(turn.get("plans") or []), plan],
        "retry": None,
    }
    max_steps = services.settings.agent.max_steps_per_question
    step_index = turn.get("step_index", 0)
    if plan.get("final", True):
        return {"turn": {**turn, "loop_outcome": "answer"}}
    if step_index + 1 >= max_steps:
        note = services.prompts.reply("notes.steps_missing", max_steps=max_steps)
        notes = [*(turn.get("notes") or []), note]
        return {"turn": {**turn, "notes": notes, "loop_outcome": "answer"}}
    return {
        "turn": {
            **turn,
            "step_index": step_index + 1,
            "attempt": 0,
            "next": "write_sql",
        }
    }
