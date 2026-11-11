"""The agent graph of the walking skeleton (spec 01), with the query safety of spec 30.

    load_context -> write_sql -> guard -> run_query -> write_answer -> scan_output -> finish
                                              |  0 rows        ^
                                              +-> check_empty -+

Each step can end the turn early with a final answer (a refusal, a block or an error message). Every
final answer goes through `scan_output`. Spec 31 replaces this graph with the full agent core.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph

from opsfleet_agent.agent.nodes.skeleton import (
    check_empty,
    finish,
    guard,
    load_context,
    route_after,
    route_after_query,
    run_query,
    scan_output,
    write_answer,
    write_sql,
)
from opsfleet_agent.agent.state import AgentState, TurnContext

PROMPT_DIR = Path(__file__).parent / "prompts"


def load_prompts() -> dict[str, str]:
    return {p.stem: p.read_text(encoding="utf-8") for p in PROMPT_DIR.glob("*.md")}


def build_graph(checkpointer: Any | None = None) -> Any:
    g = StateGraph(AgentState, context_schema=TurnContext)
    g.add_node("load_context", load_context)
    g.add_node("write_sql", write_sql)
    g.add_node("guard", guard)
    g.add_node("run_query", run_query)
    g.add_node("check_empty", check_empty)
    g.add_node("write_answer", write_answer)
    g.add_node("scan_output", scan_output)
    g.add_node("finish", finish)

    g.add_edge(START, "load_context")
    g.add_edge("load_context", "write_sql")
    for step, nxt in (
        ("write_sql", "guard"),
        ("guard", "run_query"),
        ("check_empty", "write_answer"),
    ):
        g.add_conditional_edges(
            step, route_after, {"continue": nxt, "finish": "scan_output"}
        )
    g.add_conditional_edges(
        "run_query",
        route_after_query,
        {"continue": "write_answer", "empty": "check_empty", "finish": "scan_output"},
    )
    g.add_edge("write_answer", "scan_output")
    g.add_edge("scan_output", "finish")
    g.add_edge("finish", END)
    return g.compile(checkpointer=checkpointer)
