"""`finish`: add the turn to the window and update the pinned facts (specs 31 and 34).

Code writes the pinned facts from structured outputs only: the `active_filters` and
`definitions_used` of `write_sql`, the saved and deleted reports, and the result references of the
last data turn. The route already pinned the format and the facts that the user set.

Each result of a data turn goes to the result cache with its full masked rows (at most 200). The
window keeps only a reference and a preview of 5 rows (spec 34, B2.3). The summary runs after this
node (`memory/summary.py`).
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from opsfleet_agent.agent.formatting import ResultData, format_value
from opsfleet_agent.agent.nodes.common import INTERNAL_MESSAGE, node, turn_of
from opsfleet_agent.agent.state import AgentState, TurnContext, TurnState
from opsfleet_agent.memory import result_cache
from opsfleet_agent.memory.messages import Message
from opsfleet_agent.memory.pinned import (
    MAX_LAST_RESULTS,
    with_reports,
    with_sql_facts,
)

STEP_FIELDS = ("purpose", "sql", "job_id", "bytes_billed", "entities")


def preview_rows(result: ResultData, rows: int) -> list[list[str]]:
    """The first rows of a result, formatted as the user saw them."""
    return [
        [format_value(v, k) for v, k in zip(row, result["kinds"], strict=True)]
        for row in result["rows"][:rows]
    ]


def cache_results(
    state: AgentState, ctx: TurnContext, turn: TurnState, turn_no: int
) -> tuple[list[dict[str, Any]], list[Message]]:
    """Store the results of a data answer. Returns the pinned `last_results` and the
    `result_ref` messages for the window."""
    if turn.get("intent") != "analysis" or not turn.get("results") or ctx.store is None:
        return [], []
    settings = ctx.services.settings.context
    steps = {
        s.get("result_ref"): s for s in turn.get("steps") or [] if s.get("result_ref")
    }
    plans = list(turn.get("plans") or [])
    now = ctx.services.clock()
    items: list[dict[str, Any]] = []
    messages: list[Message] = []
    for index, result in enumerate(list(turn.get("results") or [])[-MAX_LAST_RESULTS:]):
        step = dict(steps.get(result["ref"]) or {})
        ref = result_cache.put(
            ctx.store,
            result,
            owner_id=ctx.user.id,
            conversation_id=state.get("conversation_id", ""),
            turn_no=turn_no,
            sql=str(step.get("sql") or ""),
            now=now,
            days=settings.result_cache_days,
        )
        items.append(
            {
                "ref": ref,
                "turn": turn_no,
                "purpose": result["purpose"],
                "question": turn.get("question", ""),
                "step": {k: step[k] for k in STEP_FIELDS if k in step},
                "plan": plans[index] if index < len(plans) else {},
            }
        )
        messages.append(
            Message(
                role="tool",
                turn_no=turn_no,
                kind="result_ref",
                text=result["purpose"],
                ref=ref,
                columns=list(result["columns"]),
                row_count=int(result["row_count"]),
                preview=preview_rows(result, settings.result_preview_rows),
            )
        )
    return items, messages


def turn_messages(
    turn: TurnState, answer: str, turn_no: int, refs: list[Message]
) -> list[Message]:
    """The messages of the turn after the question: the result references, a delete
    confirmation with the typed answer, and the answer."""
    messages: list[Message] = list(refs)
    if turn.get("confirmation_prompt"):
        messages.append(
            Message(
                role="assistant",
                turn_no=turn_no,
                kind="confirmation",
                text=turn["confirmation_prompt"],
            )
        )
    if turn.get("confirm_text") and not turn.get("replay_input"):
        messages.append(
            Message(
                role="user",
                turn_no=turn_no,
                kind="confirmation",
                text=turn["confirm_text"],
            )
        )
    kind = "answer" if turn.get("intent") in ("analysis", "report") else "reply"
    messages.append(Message(role="assistant", turn_no=turn_no, kind=kind, text=answer))
    return messages


def update_pinned(
    pinned: dict[str, Any] | None, turn: TurnState, turn_no: int
) -> dict[str, Any]:
    out = with_sql_facts(pinned, list(turn.get("plans") or []))
    report = turn.get("report") or {}
    delete = turn.get("delete") or {}
    created = None
    if report.get("saved_id"):
        title = (report.get("built") or {}).get("title") or report.get("title") or ""
        created = (report["saved_id"], title)
    outcome = delete.get("outcome")
    return with_reports(
        out,
        turn_no,
        created=created,
        deleted=list(delete.get("report_ids") or []) if outcome == "deleted" else None,
        restored=list(delete.get("report_ids") or [])
        if outcome == "undo_restored"
        else None,
    )


@node("finish")
def finish(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    ctx = runtime.context
    turn = turn_of(state)
    turn_no = int(state.get("turn_no") or 0)
    answer = turn.get("final_text") or turn.get("answer") or INTERNAL_MESSAGE
    pinned = update_pinned(state.get("pinned"), turn, turn_no)
    items, refs = cache_results(state, ctx, turn, turn_no)
    if items:
        pinned["last_results"] = items
    return {
        "messages": turn_messages(turn, answer, turn_no, refs),
        "pinned": pinned,
        "turn": {**turn, "final_text": answer},
    }
