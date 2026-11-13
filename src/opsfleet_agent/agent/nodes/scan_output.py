"""`scan_output`: the last checks on every final text (spec 30, B2.6 and spec 31, B2.7 #6).

1. The prompt-leak check: an answer with a run of 12 or more words of a protected prompt is
   replaced by a prepared reply. For a request for personal data, the reply is the refusal of
   personal data: a refusal that repeats the list of the safety rules is not a real leak, and the
   user needs the allowed alternative (spec 50 round 1).
2. The output scanner of spec 30: personal data becomes `[removed]`, with one note.
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime
from opentelemetry import trace

from opsfleet_agent.agent.nodes.common import INTERNAL_MESSAGE, node, turn_of
from opsfleet_agent.agent.personal_data import asks_personal_data
from opsfleet_agent.agent.state import AgentState, TurnContext


@node("scan_output")
def scan_output(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    services = runtime.context.services
    turn = turn_of(state)
    answer = turn.get("answer") or INTERNAL_MESSAGE
    leak = services.prompts.leaks_prompt(answer)
    trace.get_current_span().set_attribute("prompt_leak", leak)
    if leak:
        kind = (
            "personal_data"
            if asks_personal_data(turn.get("question") or "")
            else "prompt_request"
        )
        answer = services.prompts.reply(f"unsafe.{kind}")
    scanned = services.scanner.scan(answer)
    final = scanned.with_note() if scanned.found else answer
    out = {**turn, "answer": final, "final_text": final, "prompt_leak": leak}
    if scanned.found:
        out["scanner_hits"] = sum(scanned.hits.values())
    return {"turn": out}
