"""`write_sql`: the model writes one query step as structured output (spec 31, B2.3)."""

from __future__ import annotations

import re
from typing import Any

from langgraph.runtime import Runtime
from pydantic import BaseModel, Field

from opsfleet_agent.agent.nodes.common import (
    is_manager,
    node,
    require_bigquery,
    scope_text,
    turn_of,
)
from opsfleet_agent.agent.nodes.loop_control import with_step
from opsfleet_agent.agent.periods import Calendar
from opsfleet_agent.agent.personal_data import asks_personal_data
from opsfleet_agent.agent.state import AgentState, TurnContext, TurnState
from opsfleet_agent.memory.context_builder import ModelRequest, builder_of
from opsfleet_agent.schema import Schema, model_schema_text

STEP_BLOCK_ROWS = 50


class ActiveFilters(BaseModel):
    period: str | None = None
    brands: list[str] = Field(default_factory=list)
    other: list[str] = Field(default_factory=list)


class SqlPlan(BaseModel):
    purpose: str = ""
    sql: str | None = None
    final: bool = True
    definitions_used: list[str] = Field(default_factory=list)
    active_filters: ActiveFilters = Field(default_factory=ActiveFilters)
    assumptions: list[str] = Field(default_factory=list)
    cannot_answer: str | None = None


def calendar_text(calendar: Calendar) -> str:
    lines = [f"today: {calendar.today.isoformat()}"]
    for name, period in calendar.named().items():
        lines.append(
            f"{name}: {period.start.isoformat()}..{period.end.isoformat()} ({period.text()}); "
            f"SQL: created_at >= TIMESTAMP('{period.start.isoformat()}') AND "
            f"created_at < TIMESTAMP('{period.end_exclusive.isoformat()}')"
        )
    return "\n".join(lines)


def tables_in(sql: str, schema: Schema) -> list[str]:
    lower = sql.lower()
    return [t for t in schema.tables if re.search(rf"\b{re.escape(t)}\b", lower)]


def retry_block(turn: TurnState, schema: Schema, manager: bool) -> str:
    retry = turn.get("retry")
    if not retry:
        return ""
    columns = "\n".join(
        f"{t}: {', '.join(schema.visible_columns(t, manager))}"
        for t in tables_in(retry["sql"], schema)
    )
    return (
        "<retry>\n"
        f"Failed SQL:\n{retry['sql']}\n"
        f"Error type: {retry['error_type']}\n"
        f"Error: {retry['message']}\n"
        f"Columns of the tables in the failed SQL:\n{columns or '(none)'}\n"
        "</retry>"
    )


def build_request(
    state: AgentState, ctx: TurnContext, calendar: Calendar, system: str
) -> ModelRequest:
    """The context of `write_sql` (spec 34, B2.1): schema, calendar and definitions first, then
    the pinned facts, the summary, the recent turns, the step, the data blocks of the earlier
    steps, the retry feedback and the question."""
    services = ctx.services
    turn = turn_of(state)
    manager = is_manager(ctx.user)
    first = services.values.first_date("order_items")
    stable = [
        (
            "<schema>\n"
            f"{model_schema_text(services.schema, manager=manager)}\n"
            f"The data starts on {first.isoformat() if first else 'an unknown date'}.\n"
            "</schema>"
        ),
        f"<calendar>\n{calendar_text(calendar)}\n</calendar>",
        f"<definitions>\n{services.prompts.text('definitions')}\n</definitions>",
    ]
    max_steps = services.settings.agent.max_steps_per_question
    if turn.get("single_step"):  # a report query (spec 32): one query, no next step
        step = "This is one query of a report. Write only this query."
    else:
        step = f"This is query step {turn.get('step_index', 0) + 1} of at most {max_steps}."
    return builder_of(services).build(
        "write_sql",
        system=system,
        state=state,
        stable=stable,
        middle=[step],
        results=list(turn.get("results") or []),
        data_rows=STEP_BLOCK_ROWS,
        retry=retry_block(turn, services.schema, manager),
        user_message=turn["question"],
    )


@node("write_sql")
def write_sql(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    ctx = runtime.context
    require_bigquery(ctx)
    lib = ctx.services.prompts
    turn = turn_of(state)
    calendar = Calendar.now()
    last_month = calendar.last_month
    system = lib.system(
        "write_sql",
        today=calendar.today.isoformat(),
        scope=scope_text(ctx.user),
        max_steps=ctx.services.settings.agent.max_steps_per_question,
        last_month_start=last_month.start.isoformat(),
        last_month_end_exclusive=last_month.end_exclusive.isoformat(),
        last_month_text=last_month.text(),
    )
    request = build_request(state, ctx, calendar, system)
    result = ctx.services.llm.generate(
        ctx.budget,
        system=request.system,
        prompt=request.prompt,
        schema=SqlPlan,
        purpose="write_sql",
        prompt_version=lib.version("safety", "write_sql", "definitions"),
    )
    plan: SqlPlan = result.parsed
    attempt = turn.get("attempt", 0) + 1
    turn = {**turn, "attempt": attempt, "plan": plan.model_dump()}
    sql = (plan.sql or "").strip()
    no_query = bool(plan.cannot_answer or not sql)
    if (
        no_query
        and not turn.get("results")
        and asks_personal_data(turn.get("question") or "")
    ):
        # Spec 30 A2.4: the refusal of personal data with the alternative, written by code. The
        # model's own refusal often repeats the list of the safety rules (spec 50 round 1).
        return {
            "turn": {
                **turn,
                "loop_outcome": "policy_block",
                "block_message": lib.reply("unsafe.personal_data"),
            }
        }
    if plan.cannot_answer or not sql:
        reason = plan.cannot_answer or "No query can answer this question."
        note = lib.reply("notes.cannot_answer", reason=reason)
        return {
            "turn": {
                **turn,
                "cannot_answer": reason,
                "notes": [*(turn.get("notes") or []), note],
                "loop_outcome": "answer",
            }
        }
    turn = with_step(turn, purpose=plan.purpose, sql=sql, attempts=attempt)
    return {"turn": {**turn, "sql": sql, "next": "guard"}}
