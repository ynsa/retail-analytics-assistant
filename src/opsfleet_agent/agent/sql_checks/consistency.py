"""The checks of one SQL step before it runs: the period and the definitions (spec 31, B7).

Both checks read the model's SQL (the qualified SQL of the guard, before the scope rewrite), so a
manager's SQL and a CEO's SQL get the same result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from opsfleet_agent.agent.context import PromptLibrary
from opsfleet_agent.agent.question_periods import named_periods
from opsfleet_agent.agent.sql_checks.date_filters import read_date_filters
from opsfleet_agent.agent.sql_checks.definition_check import (
    DefinitionVerdict,
    check_definitions,
)
from opsfleet_agent.agent.sql_checks.period_check import PeriodVerdict, check_period
from opsfleet_agent.agent.sql_checks.status_filters import read_status_filters
from opsfleet_agent.guard.sql_guard import GuardResult
from opsfleet_agent.schema import Schema

_EXAMPLES = {
    frozenset({"Cancelled", "Returned"}): "oi.status NOT IN ('Cancelled', 'Returned')",
    frozenset({"Cancelled"}): "oi.status != 'Cancelled'",
}


@dataclass(frozen=True)
class StepCheck:
    period: PeriodVerdict
    definitions: DefinitionVerdict
    feedback: str  # empty when both checks pass

    @property
    def period_text(self) -> str:
        p = self.period.period
        return f"{p.start.isoformat()}..{p.end.isoformat()}" if p else ""


def check_step(
    plan: dict,
    question: str,
    guard_result: GuardResult,
    schema: Schema,
    lib: PromptLibrary,
    today: date,
    first: date | None,
) -> StepCheck:
    sql = guard_result.qualified_sql or ""
    period = check_period(
        plan,
        read_date_filters(sql, schema) if sql else None,
        guard_result.tables,
        named_periods(question, today),
        today,
        first,
    )
    definitions = check_definitions(
        list(plan.get("definitions_used") or []),
        list(guard_result.output_columns),
        lib.definitions,
        read_status_filters(sql) if sql else None,
        reads_order_items="order_items" in guard_result.tables,
    )
    parts: list[str] = []
    if period.status == "mismatch":
        parts.append(lib.reply(f"retry.period.{period.code}", **period.values))
    for key in definitions.missing:
        definition = lib.definitions[key]
        statuses = sorted(definition.excludes)
        parts.append(
            lib.reply(
                "retry.definition",
                key=key,
                meaning=definition.meaning,
                statuses=" and ".join(statuses),
                example=_EXAMPLES.get(
                    definition.excludes,
                    f"oi.status NOT IN ({', '.join(repr(s) for s in statuses)})",
                ),
            )
        )
    return StepCheck(period, definitions, " ".join(parts))
