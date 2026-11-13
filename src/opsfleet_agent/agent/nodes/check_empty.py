"""`check_empty`: the empty-result checks of spec 30 (B2.7) in the data loop.

An `error` goes back to `write_sql` with the checker's feedback. A `true_zero` or an `unknown`
adds a note for the answer prompt, and the empty result goes on to `mask_result`.
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from opsfleet_agent.agent.nodes.common import node, turn_of
from opsfleet_agent.agent.nodes.guard_sql import guard_result_for
from opsfleet_agent.agent.nodes.loop_control import fail_attempt
from opsfleet_agent.agent.state import AgentState, TurnContext
from opsfleet_agent.errors import ErrorType
from opsfleet_agent.guard.empty_result import DiagnosisKind


@node("check_empty")
def check_empty(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    ctx = runtime.context
    turn = turn_of(state)
    diagnosis = ctx.empty_checker.diagnose(guard_result_for(runtime, turn), ctx.budget)
    if diagnosis.kind is DiagnosisKind.ERROR:
        max_attempts = ctx.services.settings.agent.max_attempts_per_step
        feedback = diagnosis.model_feedback or diagnosis.user_note
        return {
            "turn": fail_attempt(turn, ErrorType.EMPTY_RESULT, feedback, max_attempts)
        }
    notes = [*(turn.get("notes") or []), diagnosis.user_note]
    return {"turn": {**turn, "notes": notes, "next": "mask_result"}}
