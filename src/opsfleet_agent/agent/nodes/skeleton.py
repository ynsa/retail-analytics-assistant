"""Nodes of the walking skeleton (spec 01): write SQL, guard, run the query, write the answer.

Spec 31 replaces them with the full agent core. Each node runs in a span. An AgentError ends the
turn with a one-sentence message for the user, and the chat continues.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from functools import wraps
from typing import Any

import pandas as pd
from langgraph.runtime import Runtime
from pydantic import BaseModel

from opsfleet_agent.agent.state import AgentState, TurnContext, TurnState
from opsfleet_agent.errors import AgentError, ErrorType
from opsfleet_agent.guard.minimal import GuardOutcome, check_read_only
from opsfleet_agent.tracing import current_trace_id, span

INTERNAL_MESSAGE = (
    "Something went wrong on my side, so I could not answer. Please try again."
)
PARSE_MESSAGE = (
    "I could not build a valid query for this question. Please ask it in another way."
)
EMPTY_MESSAGE = "The query returned no rows for this question."
HISTORY_TURNS = 3


class SqlDraft(BaseModel):
    sql: str | None
    explanation: str
    reply: str | None


def _turn(state: AgentState) -> TurnState:
    return dict(state.get("turn") or {})  # type: ignore[return-value]


def node(name: str) -> Callable:
    """Run a node in a span. Turn an error into a final answer, so the chat does not stop."""

    def decorate(
        fn: Callable[[AgentState, Runtime[TurnContext]], dict[str, Any]],
    ) -> Callable:
        @wraps(fn)
        def wrapper(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
            try:
                with span(f"node.{name}", "node"):
                    return fn(state, runtime)
            except AgentError as exc:
                return {
                    "turn": {
                        **_turn(state),
                        "error_type": exc.error_type.value,
                        "answer": exc.user_message,
                    }
                }
            except Exception:  # noqa: BLE001 - the chat must continue after any bug
                return {
                    "turn": {
                        **_turn(state),
                        "error_type": ErrorType.INTERNAL_ERROR.value,
                        "answer": INTERNAL_MESSAGE,
                    }
                }

        return wrapper

    return decorate


def _history(state: AgentState) -> str:
    messages = state.get("messages") or []
    recent = messages[
        -(HISTORY_TURNS * 2 + 1) : -1
    ]  # the last turns before the current question
    if not recent:
        return "(none)"
    return "\n".join(f"{m['role']}: {m['text'][:500]}" for m in recent)


def _format_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "(no rows)"
    cols = [str(c) for c in frame.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for row in frame.itertuples(index=False):
        lines.append(
            "| " + " | ".join("" if pd.isna(v) else str(v) for v in row) + " |"
        )
    return "\n".join(lines)


@node("load_context")
def load_context(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    question = state["messages"][-1]["text"]
    return {"turn": {"question": question, "trace_id": current_trace_id() or ""}}


@node("write_sql")
def write_sql(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    ctx = runtime.context
    turn = _turn(state)
    prompt = ctx.services.prompts["write_sql"].format(
        schema=ctx.services.schema_text,
        today=datetime.now(UTC).strftime("%Y-%m-%d"),
        history=_history(state),
        question=turn["question"],
    )
    result = ctx.services.llm.generate(
        ctx.budget,
        system="You write BigQuery SQL for analysts.",
        prompt=prompt,
        schema=SqlDraft,
        purpose="write_sql",
    )
    draft: SqlDraft = result.parsed
    if not draft.sql:
        return {
            "turn": {
                **turn,
                "answer": draft.reply
                or "I can only answer questions about the sales data.",
            }
        }
    return {"turn": {**turn, "sql": draft.sql, "explanation": draft.explanation}}


@node("guard")
def guard(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    settings = runtime.context.services.settings
    turn = _turn(state)
    with span("guard.check", "guard") as sp:
        result = check_read_only(
            turn["sql"], settings.bigquery.dataset, settings.bigquery.tables
        )
        sp.set_attribute("outcome", result.outcome.value)
        if result.reason_code:
            sp.set_attribute("reason_code", result.reason_code)
    if result.outcome is GuardOutcome.POLICY_BLOCK:
        return {
            "turn": {
                **turn,
                "error_type": ErrorType.GUARD_POLICY_BLOCK.value,
                "answer": result.user_message,
            }
        }
    if result.outcome is GuardOutcome.PARSE_ERROR:
        return {
            "turn": {
                **turn,
                "error_type": ErrorType.GUARD_PARSE_ERROR.value,
                "answer": PARSE_MESSAGE,
            }
        }
    return {"turn": turn}


@node("run_query")
def run_query(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    ctx = runtime.context
    turn = _turn(state)
    result = ctx.services.bq.query(
        turn["sql"], ctx.budget, user_id=ctx.user.id, purpose="answer"
    )
    if result.row_count == 0:
        return {
            "turn": {
                **turn,
                "row_count": 0,
                "error_type": ErrorType.EMPTY_RESULT.value,
                "answer": EMPTY_MESSAGE,
            }
        }
    return {
        "turn": {
            **turn,
            "result_table": _format_table(result.dataframe),
            "row_count": result.row_count,
            "truncated": result.truncated,
        }
    }


@node("write_answer")
def write_answer(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    ctx = runtime.context
    turn = _turn(state)
    prompt = ctx.services.prompts["write_answer"].format(
        question=turn["question"],
        explanation=turn.get("explanation", ""),
        row_count=turn.get("row_count", 0),
        truncated_note=", more rows were cut" if turn.get("truncated") else "",
        result_table=turn.get("result_table", ""),
    )
    result = ctx.services.llm.generate(
        ctx.budget,
        system="You explain data to managers in plain English.",
        prompt=prompt,
        purpose="write_answer",
    )
    return {"turn": {**turn, "answer": result.text.strip() or INTERNAL_MESSAGE}}


def finish(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    answer = _turn(state).get("answer") or INTERNAL_MESSAGE
    return {"messages": [{"role": "assistant", "text": answer}]}


def route_after(state: AgentState) -> str:
    """Go to the end as soon as a node has set the final answer."""
    return "finish" if _turn(state).get("answer") else "continue"
