"""`run_query`: run the scoped SQL with a dry run and the byte cap (spec 31, B2.1).

The raw result stays in the turn scratch. `mask_result` masks it before it goes into the state, so
raw rows never reach the checkpoint (spec 30, B2.5).
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from opsfleet_agent.agent.nodes.common import INTERNAL_MESSAGE, node, turn_of
from opsfleet_agent.agent.nodes.guard_sql import guard_result_for
from opsfleet_agent.agent.nodes.loop_control import RETRYABLE, fail_attempt, with_step
from opsfleet_agent.agent.state import AgentState, TurnContext
from opsfleet_agent.errors import AgentError, ErrorType


def _feedback(exc: AgentError, runtime: Runtime[TurnContext]) -> str:
    if exc.error_type is ErrorType.BQ_BYTES_CAP:
        return runtime.context.services.prompts.reply("retry.bq_bytes_cap")
    return exc.detail or exc.user_message


@node("run_query")
def run_query(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    ctx = runtime.context
    turn = turn_of(state)
    checked = guard_result_for(runtime, turn)
    if (
        not checked.passed or checked.sql is None
    ):  # never run SQL that the guard did not pass
        raise AgentError(ErrorType.INTERNAL_ERROR, INTERNAL_MESSAGE, "unchecked SQL")
    try:
        result = ctx.services.bq.query(
            checked.sql,
            ctx.budget,
            params=checked.params,
            user_id=ctx.user.id,
            purpose="answer",
        )
    except AgentError as exc:
        if exc.error_type not in RETRYABLE:
            raise  # bq_permission, bq_timeout, bq_error, limit_exceeded: the turn ends
        max_attempts = ctx.services.settings.agent.max_attempts_per_step
        return {
            "turn": fail_attempt(
                turn, exc.error_type, _feedback(exc, runtime), max_attempts
            )
        }
    ctx.scratch.setdefault("executed_sql", []).append(checked.sql)
    turn = with_step(
        turn,
        row_count=result.row_count,
        job_id=result.job_id or "",
        bytes_billed=int(result.bytes_billed or 0),
        tables=sorted(checked.tables),
    )
    ctx.scratch["query_result"] = result  # masked by `mask_result`, never stored raw
    if result.row_count == 0:
        return {"turn": {**turn, "row_count": 0, "next": "check_empty"}}
    return {"turn": {**turn, "row_count": result.row_count, "next": "mask_result"}}
