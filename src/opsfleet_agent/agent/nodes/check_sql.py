"""`check_sql`: the period and definition checks between the guard and the query (spec 31, B7).

The Docker check of 2026-11-12 found answers that stated a period and a definition that the query
did not use. This node compares the model's SQL with the period of `active_filters` and with the
status filter of each cited definition.

- A clear mismatch goes back to `write_sql` with exact feedback, in the normal attempt budget
  (`fail_attempt`, error type `sql_mismatch`). The SQL does not run.
- After the last attempt, the step fails with the `give_up` message. This is safer than a footer
  with the period of the SQL: the answer text of the model would still name the asked period.
- "Cannot verify" (a filter form that the code cannot read) passes. The span attributes
  `period_check` and `definition_check` show it.
- A report query (spec 32) gets only the definition check. Its SQL comes from the report plan,
  and a report often reads a trend outside the report period, so the period check is skipped.
  A definition mismatch goes back to `write_sql` for this one query (spec 50 round 2, RP-05).

The step record keeps the period and the definition keys that passed. The footer uses them.
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime
from opentelemetry import trace

from opsfleet_agent.agent.nodes.common import node, turn_of
from opsfleet_agent.agent.nodes.guard_sql import guard_result_for
from opsfleet_agent.agent.nodes.loop_control import fail_attempt, with_step
from opsfleet_agent.agent.sql_checks.consistency import check_step
from opsfleet_agent.agent.state import AgentState, TurnContext
from opsfleet_agent.errors import ErrorType

MAX_RANGES_ATTRIBUTE = 6


@node("check_sql")
def check_sql(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    ctx = runtime.context
    services = ctx.services
    turn = turn_of(state)
    sp = trace.get_current_span()
    report_query = bool(turn.get("single_step"))
    plan = turn.get("plan") or {}
    try:
        result = check_step(
            plan,
            turn.get("question") or "",
            guard_result_for(runtime, turn),
            services.schema,
            services.prompts,
            services.clock().date(),
            services.values.first_date("order_items"),
            with_period=not report_query,
        )
    except Exception as exc:  # noqa: BLE001 - "cannot verify" is a pass, never a block
        sp.add_event("check_failed", {"error": type(exc).__name__})
        status = "skipped" if report_query else "unverified"
        sp.set_attribute("period_check", status)
        turn = with_step(turn, period_check=status, definition_check="unverified")
        return {"turn": {**turn, "next": "run_query"}}
    sp.set_attribute("period_check", result.period.status)
    if result.period.code:
        sp.set_attribute("period_check_code", result.period.code)
    if result.period_text:
        sp.set_attribute("period", result.period_text)
    if result.period.sql_ranges:
        sp.set_attribute(
            "sql_periods", list(result.period.sql_ranges[:MAX_RANGES_ATTRIBUTE])
        )
    sp.set_attribute("definition_check", result.definitions.status)
    if result.definitions.missing:
        sp.set_attribute("definitions_missing", list(result.definitions.missing))
    turn = with_step(
        turn,
        period=result.period_text,
        period_check=result.period.status,
        definitions=list(result.definitions.applied),
        definition_check=result.definitions.status,
    )
    if result.feedback:
        max_attempts = services.settings.agent.max_attempts_per_step
        return {
            "turn": fail_attempt(
                turn, ErrorType.SQL_MISMATCH, result.feedback, max_attempts
            )
        }
    return {"turn": {**turn, "next": "run_query"}}
