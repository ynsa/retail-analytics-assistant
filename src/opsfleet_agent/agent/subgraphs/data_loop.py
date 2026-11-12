"""The subgraph `data_loop`: write_sql -> guard -> check_sql -> run_query -> [check_empty] ->
mask_result.

It runs the query steps of one question with the bounded self-correction (spec 31, B2.4). It ends
in one of these states:

- `turn.loop_outcome = "answer"`: the results (or a `cannot_answer` reason) are ready.
- `turn.loop_outcome = "give_up"`: a step used all its attempts.
- `turn.loop_outcome = "policy_block"`: the guard blocked the request.
- `turn.answer` is set: an error that cannot be corrected ended the turn.

The report flow (spec 32) uses the same subgraph for each report query. A report query has its
SQL already, so the report flow sets `turn.next = "guard"` and `turn.single_step = true`: the loop
starts at the guard and ends after one step.
"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from opsfleet_agent.agent.nodes.check_empty import check_empty
from opsfleet_agent.agent.nodes.check_sql import check_sql
from opsfleet_agent.agent.nodes.common import turn_of
from opsfleet_agent.agent.nodes.guard_sql import guard
from opsfleet_agent.agent.nodes.mask_result import mask_result
from opsfleet_agent.agent.nodes.run_query import run_query
from opsfleet_agent.agent.nodes.write_sql import write_sql
from opsfleet_agent.agent.state import AgentState, TurnContext, TurnState

LOOP_NODES = (
    "write_sql",
    "guard",
    "check_sql",
    "run_query",
    "check_empty",
    "mask_result",
)


class LoopOutput(TypedDict):
    """The subgraph returns only the turn. Without this, the parent graph would add the
    `messages` of the subgraph state a second time (the reducer appends)."""

    turn: TurnState


def next_in_loop(state: AgentState) -> str:
    turn = turn_of(state)
    if turn.get("answer") or turn.get("loop_outcome"):
        return END
    nxt = turn.get("next")
    return nxt if nxt in LOOP_NODES else END


def loop_entry(state: AgentState) -> str:
    """`write_sql` for a question. `guard` for a report query with its SQL (spec 32)."""
    turn = turn_of(state)
    if turn.get("next") == "guard" and turn.get("sql"):
        return "guard"
    return "write_sql"


def build_data_loop() -> Any:
    g = StateGraph(AgentState, context_schema=TurnContext, output_schema=LoopOutput)
    g.add_node("write_sql", write_sql)
    g.add_node("guard", guard)
    g.add_node("check_sql", check_sql)
    g.add_node("run_query", run_query)
    g.add_node("check_empty", check_empty)
    g.add_node("mask_result", mask_result)
    g.add_conditional_edges(
        START, loop_entry, {"write_sql": "write_sql", "guard": "guard"}
    )
    targets = {name: name for name in LOOP_NODES} | {END: END}
    for name in LOOP_NODES:
        g.add_conditional_edges(name, next_in_loop, targets)
    return g.compile()


def next_after_loop(state: AgentState) -> str:
    turn = turn_of(state)
    if turn.get("answer"):
        return "finish"
    outcome = turn.get("loop_outcome")
    if outcome == "give_up":
        return "give_up"
    if outcome == "policy_block":
        return "explain_block"
    return "write_answer"
