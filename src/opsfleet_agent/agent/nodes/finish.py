"""`finish`: add the answer to the conversation and update the pinned facts (spec 31, B2.1).

Code writes the pinned facts from structured outputs only: the format request of the route, and
the `active_filters` and `definitions_used` of `write_sql`. The latest value of each key wins.
Spec 34 extends this node with the summary and the turn record, and spec 35 with the statistics.
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from opsfleet_agent.agent.nodes.common import INTERNAL_MESSAGE, node, turn_of
from opsfleet_agent.agent.state import AgentState, TurnContext, TurnState

MAX_PINNED_DEFINITIONS = 10


def update_pinned(pinned: dict[str, Any] | None, turn: TurnState) -> dict[str, Any]:
    out = dict(pinned or {})
    fmt = (turn.get("route") or {}).get("format_request")
    if fmt:
        out["format"] = fmt
    filters = dict(out.get("active_filters") or {})
    definitions = list(out.get("definitions_in_use") or [])
    for plan in turn.get("plans") or []:
        for key, value in (plan.get("active_filters") or {}).items():
            if value:
                filters[key] = value
        definitions += [
            k for k in plan.get("definitions_used") or [] if k not in definitions
        ]
    if filters:
        out["active_filters"] = filters
    if definitions:
        out["definitions_in_use"] = definitions[-MAX_PINNED_DEFINITIONS:]
    return out


@node("finish")
def finish(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    turn = turn_of(state)
    answer = turn.get("final_text") or turn.get("answer") or INTERNAL_MESSAGE
    return {
        "messages": [{"role": "assistant", "text": answer}],
        "pinned": update_pinned(state.get("pinned"), turn),
        "turn": {**turn, "final_text": answer},
    }
