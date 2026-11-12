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
LAST_DATA_STEP_FIELDS = (
    "purpose",
    "sql",
    "result_ref",
    "job_id",
    "bytes_billed",
    "entities",
)


def last_data(state: AgentState, turn: TurnState) -> dict[str, Any] | None:
    """The data of an answer with results, for "save this as a report" (spec 32, A2.5).
    Only masked results go into the state. Spec 34 replaces it with the result cache."""
    if turn.get("intent") != "analysis" or not turn.get("results"):
        return None
    steps = [
        {k: step[k] for k in LAST_DATA_STEP_FIELDS if k in step}  # type: ignore[literal-required]
        for step in turn.get("steps") or []
        if step.get("result_ref")
    ]
    return {
        "question": turn.get("question", ""),
        "results": list(turn.get("results") or []),
        "plans": list(turn.get("plans") or []),
        "steps": steps,
        "conversation_id": state.get("conversation_id", ""),
    }


def turn_messages(turn: TurnState, answer: str) -> list[dict[str, str]]:
    """The messages of the turn. A delete confirmation adds the list and the typed answer."""
    messages: list[dict[str, str]] = []
    if turn.get("confirmation_prompt"):
        messages.append({"role": "assistant", "text": turn["confirmation_prompt"]})
    if turn.get("confirm_text") and not turn.get("replay_input"):
        messages.append({"role": "user", "text": turn["confirm_text"]})
    messages.append({"role": "assistant", "text": answer})
    return messages


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
    update: dict[str, Any] = {
        "messages": turn_messages(turn, answer),
        "pinned": update_pinned(state.get("pinned"), turn),
        "turn": {**turn, "final_text": answer},
    }
    data = last_data(state, turn)
    if data:
        update["last_data"] = data
    return update
