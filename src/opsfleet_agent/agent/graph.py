"""The LangGraph graph of one turn (spec 31, B2.1). It replaces the walking skeleton of spec 01.

    load_context -> route -> prepared_reply ------------------------------> scan_output -> finish
                                                                                  -> summarize
                          -> schema_answer ------------------------------->
                          -> data_loop -> write_answer -------------------->
                                       -> give_up | explain_block -------->
                          -> report_flow | delete_flow (specs 32, 33) ---->

`data_loop` is a subgraph: write_sql -> guard -> check_sql -> run_query -> [check_empty] ->
mask_result, with the bounded self-correction. Every final text goes through `scan_output`. A node that ends the turn
early (an error, a refusal) sets `turn.answer`, and the graph goes to `scan_output`.

`summarize` (spec 34) runs after `finish`, when the window has more than 8 turns. The CLI shows
the answer when `finish` ends, so the user does not wait for the summary.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from langgraph.graph import END, START, StateGraph

from opsfleet_agent import capabilities
from opsfleet_agent.agent.nodes.common import turn_of
from opsfleet_agent.agent.nodes.finish import finish
from opsfleet_agent.agent.nodes.load_context import load_context
from opsfleet_agent.agent.nodes.replies import explain_block, give_up, prepared_reply
from opsfleet_agent.agent.nodes.route import next_after_route, route
from opsfleet_agent.agent.nodes.scan_output import scan_output
from opsfleet_agent.agent.nodes.schema_answer import schema_answer
from opsfleet_agent.agent.nodes.write_answer import write_answer
from opsfleet_agent.agent.state import AgentState, TurnContext
from opsfleet_agent.agent.subgraphs.data_loop import build_data_loop, next_after_loop
from opsfleet_agent.memory.summary import summarize


def _after_load(state: AgentState) -> str:
    return "finish" if turn_of(state).get("answer") else "route"


def build_graph(
    checkpointer: Any | None,
    flows: Mapping[str, Callable[..., Any] | Any],
) -> Any:
    """`flows` holds the `report` and `delete` subgraphs of specs 32 and 33. Each flow must set
    `turn.answer`."""
    flow_nodes = dict(flows)
    g = StateGraph(AgentState, context_schema=TurnContext)
    g.add_node("load_context", load_context)
    g.add_node("route", route)
    g.add_node("prepared_reply", prepared_reply)
    g.add_node("schema_answer", schema_answer)
    g.add_node("data_loop", build_data_loop())
    g.add_node("write_answer", write_answer)
    g.add_node("give_up", give_up)
    g.add_node("explain_block", explain_block)
    g.add_node("report_flow", flow_nodes["report"])
    g.add_node("delete_flow", flow_nodes["delete"])
    g.add_node("scan_output", scan_output)
    g.add_node("finish", finish)
    g.add_node("summarize", summarize)

    g.add_edge(START, "load_context")
    g.add_conditional_edges(
        "load_context", _after_load, {"route": "route", "finish": "scan_output"}
    )
    g.add_conditional_edges(
        "route",
        next_after_route,
        {
            "analysis": "data_loop",
            "schema_answer": "schema_answer",
            "prepared_reply": "prepared_reply",
            "report": "report_flow",
            "delete": "delete_flow",
            "finish": "scan_output",
        },
    )
    g.add_conditional_edges(
        "data_loop",
        next_after_loop,
        {
            "write_answer": "write_answer",
            "give_up": "give_up",
            "explain_block": "explain_block",
            "finish": "scan_output",
        },
    )
    for name in (
        "prepared_reply",
        "schema_answer",
        "write_answer",
        "give_up",
        "explain_block",
        "report_flow",
        "delete_flow",
    ):
        g.add_edge(name, "scan_output")
    g.add_edge("scan_output", "finish")
    g.add_edge("finish", "summarize")
    g.add_edge("summarize", END)
    return g.compile(checkpointer=checkpointer)


capabilities.register("agent_core")  # spec 37, B2.5
