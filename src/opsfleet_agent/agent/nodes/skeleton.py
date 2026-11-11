"""Nodes of the walking skeleton (spec 01) with the query safety of spec 30.

    write_sql -> guard -> run_query (masks the rows) -> [check_empty] -> write_answer -> scan_output

Spec 31 replaces them with the full agent core. Each node runs in a span. An AgentError ends the
turn with a one-sentence message for the user, and the chat continues.

Extension points for spec 31: a `parse_error` of the guard and an `error` of the empty-result
checks store `model_feedback` in the turn state. The retry loop sends it back to the model. A
`policy_block` never goes back to the model.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import Any

from langgraph.runtime import Runtime
from pydantic import BaseModel

from opsfleet_agent.agent.state import AgentState, TurnContext, TurnState
from opsfleet_agent.config import Role
from opsfleet_agent.errors import AgentError, ErrorType
from opsfleet_agent.guard.empty_result import DiagnosisKind
from opsfleet_agent.guard.masker import MaskedResult
from opsfleet_agent.guard.sql_guard import GuardOutcome, GuardResult
from opsfleet_agent.identity import UserContext
from opsfleet_agent.schema import model_schema_text
from opsfleet_agent.tracing import current_trace_id, span

INTERNAL_MESSAGE = (
    "Something went wrong on my side, so I could not answer. Please try again."
)
PARSE_MESSAGE = (
    "I could not build a valid query for this question. Please ask it in another way."
)
EMPTY_ERROR_MESSAGE = (
    "I could not find data for this question, because the query has an error."
)
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


def _scope_text(user: UserContext) -> str:
    if user.role is Role.CEO:
        return "all brands and all customers (CEO)"
    return (
        f"only the brands {', '.join(user.brands)} (manager). Customer figures cover only "
        "the customers who bought these brands"
    )


def _guard_result(runtime: Runtime[TurnContext], turn: TurnState) -> GuardResult:
    """The guard result of this turn. After a resume from a checkpoint, check the SQL again."""
    ctx = runtime.context
    cached = ctx.scratch.get("guard_result")
    if isinstance(cached, GuardResult) and cached.source_sql == turn["sql"]:
        return cached
    result = ctx.guard.check(turn["sql"])
    ctx.scratch["guard_result"] = result
    return result


@node("load_context")
def load_context(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    question = state["messages"][-1]["text"]
    return {"turn": {"question": question, "trace_id": current_trace_id() or ""}}


@node("write_sql")
def write_sql(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    ctx = runtime.context
    turn = _turn(state)
    today = datetime.now(UTC).date()
    this_month_start = today.replace(day=1)
    last_month_start = (this_month_start - timedelta(days=1)).replace(day=1)
    prompt = ctx.services.prompts["write_sql"].format(
        schema=model_schema_text(
            ctx.services.schema, manager=ctx.user.role is Role.MANAGER
        ),
        scope=_scope_text(ctx.user),
        today=today.isoformat(),
        last_month_start=last_month_start.isoformat(),
        this_month_start=this_month_start.isoformat(),
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
    """Spec 30: the full SQL guard. The guard writes its own `guard` span."""
    turn = _turn(state)
    result = runtime.context.guard.check(turn["sql"])
    runtime.context.scratch["guard_result"] = result
    turn = {
        **turn,
        "guard_outcome": result.outcome.value,
        "reason_code": result.reason_code or "",
    }
    if result.outcome is GuardOutcome.POLICY_BLOCK:  # no retry (spec 30, B1)
        return {
            "turn": {
                **turn,
                "error_type": ErrorType.GUARD_POLICY_BLOCK.value,
                "answer": result.user_message,
            }
        }
    if (
        result.outcome is GuardOutcome.PARSE_ERROR
    ):  # spec 31 sends the feedback to the model
        return {
            "turn": {
                **turn,
                "model_feedback": result.model_feedback or "",
                "error_type": ErrorType.GUARD_PARSE_ERROR.value,
                "answer": PARSE_MESSAGE,
            }
        }
    return {"turn": turn}


def _masked_turn(turn: TurnState, masked: MaskedResult) -> TurnState:
    return {
        **turn,
        "result_table": masked.data_block("r1"),
        "row_count": masked.row_count,
        "truncated": masked.truncated,
    }


@node("run_query")
def run_query(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    """Run the scoped SQL with `@scope_brands`, then mask the rows at once: raw rows never go
    into the state or the checkpoint (spec 30, B2.5)."""
    ctx = runtime.context
    turn = _turn(state)
    checked = _guard_result(runtime, turn)
    if (
        not checked.passed or checked.sql is None
    ):  # never run SQL that the guard did not pass
        raise AgentError(ErrorType.INTERNAL_ERROR, INTERNAL_MESSAGE, "unchecked SQL")
    result = ctx.services.bq.query(
        checked.sql,
        ctx.budget,
        params=checked.params,
        user_id=ctx.user.id,
        purpose="answer",
    )
    if result.row_count == 0:
        return {
            "turn": {
                **turn,
                "row_count": 0,
                "error_type": ErrorType.EMPTY_RESULT.value,
            }
        }
    return {"turn": _masked_turn(turn, ctx.services.masker.mask(result, checked))}


@node("check_empty")
def check_empty(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    """Spec 30, B2.7: find out if the data is really empty, or if the query has an error."""
    ctx = runtime.context
    turn = _turn(state)
    diagnosis = ctx.empty_checker.diagnose(_guard_result(runtime, turn), ctx.budget)
    if diagnosis.kind is DiagnosisKind.ERROR:  # spec 31 retries with the feedback
        return {
            "turn": {
                **turn,
                "model_feedback": diagnosis.model_feedback or "",
                "notes": diagnosis.user_note,
                "answer": f"{EMPTY_ERROR_MESSAGE} {diagnosis.user_note}",
            }
        }
    empty = MaskedResult(columns=[], rows=[], row_count=0, truncated=False)
    return {"turn": {**_masked_turn(turn, empty), "notes": diagnosis.user_note}}


@node("write_answer")
def write_answer(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    ctx = runtime.context
    turn = _turn(state)
    prompt = ctx.services.prompts["write_answer"].format(
        question=turn["question"],
        explanation=turn.get("explanation", ""),
        scope=_scope_text(ctx.user),
        notes=turn.get("notes") or "(none)",
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


@node("scan_output")
def scan_output(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    """Spec 30, B2.6: the last safety net on every final answer."""
    turn = _turn(state)
    scanned = runtime.context.services.scanner.scan(turn.get("answer") or "")
    if not scanned.found:
        return {"turn": turn}
    return {
        "turn": {
            **turn,
            "answer": scanned.with_note(),
            "scanner_hits": sum(scanned.hits.values()),
        }
    }


def finish(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    answer = _turn(state).get("answer") or INTERNAL_MESSAGE
    return {"messages": [{"role": "assistant", "text": answer}]}


def route_after(state: AgentState) -> str:
    """Go to the end as soon as a node has set the final answer."""
    return "finish" if _turn(state).get("answer") else "continue"


def route_after_query(state: AgentState) -> str:
    turn = _turn(state)
    if turn.get("answer"):
        return "finish"
    return "empty" if turn.get("row_count") == 0 else "continue"
