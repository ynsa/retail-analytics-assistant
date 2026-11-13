"""`guard`: the SQL guard of spec 30 in the data loop (spec 31, B2.1 and B2.4).

- `policy_block`: the loop ends with the guard message. No retry.
- `parse_error`: the guard's `model_feedback` goes back to `write_sql`.
- `pass`: `check_sql` checks the period and the definitions, then the scoped SQL runs. The
  `GuardResult` stays in the turn scratch, not in the state.
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from opsfleet_agent.agent.nodes.common import node, turn_of
from opsfleet_agent.agent.nodes.loop_control import fail_attempt, with_step
from opsfleet_agent.agent.state import AgentState, TurnContext, TurnState
from opsfleet_agent.errors import ErrorType
from opsfleet_agent.guard.sql_guard import GuardOutcome, GuardResult

BRAND_FILTER_OPS = frozenset({"=", "in"})
# Filter columns that name a report entity (spec 32, B2.4 step 7): (table, column) -> kind.
ENTITY_COLUMNS = {
    ("products", "brand"): "brand",
    ("products", "category"): "category",
    ("products", "department"): "department",
    ("products", "name"): "product",
    ("users", "state"): "state",
    ("users", "country"): "country",
}


def brand_literals(result: GuardResult) -> list[str]:
    """The brand names of exact filters on `products.brand`, for the footer."""
    names: list[str] = []
    for lit in result.filters:
        if (
            lit.table == "products"
            and lit.column == "brand"
            and lit.op in BRAND_FILTER_OPS
            and lit.exact
        ):
            names += [v for v in lit.values if v not in names]
    return names


def entity_literals(result: GuardResult) -> dict[str, list[str]]:
    """The values of exact filters on the entity columns, for the report entities (spec 32)."""
    found: dict[str, list[str]] = {}
    for lit in result.filters:
        kind = ENTITY_COLUMNS.get((lit.table, lit.column))
        if kind and lit.op in BRAND_FILTER_OPS and lit.exact:
            values = found.setdefault(kind, [])
            values += [v for v in lit.values if v not in values]
    return found


def guard_result_for(runtime: Runtime[TurnContext], turn: TurnState) -> GuardResult:
    """The guard result of the current SQL. After a resume from a checkpoint, check it again."""
    ctx = runtime.context
    cached = ctx.scratch.get("guard_result")
    if isinstance(cached, GuardResult) and cached.source_sql == turn.get("sql"):
        return cached
    result = ctx.guard.check(turn.get("sql", ""))
    ctx.scratch["guard_result"] = result
    return result


@node("guard")
def guard(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    ctx = runtime.context
    turn = turn_of(state)
    result = ctx.guard.check(turn.get("sql", ""))  # writes its own `guard` span
    ctx.scratch["guard_result"] = result
    turn = with_step(
        turn,
        guard_outcome=result.outcome.value,
        reason_code=result.reason_code or "",
        brands=brand_literals(result),
        entities=entity_literals(result),
    )
    turn = {
        **turn,
        "guard_outcome": result.outcome.value,
        "reason_code": result.reason_code or "",
    }
    if result.outcome is GuardOutcome.POLICY_BLOCK:
        turn = with_step(turn, error_type=ErrorType.GUARD_POLICY_BLOCK.value)
        return {
            "turn": {
                **turn,
                "loop_outcome": "policy_block",
                "block_message": result.user_message or "",
                "error_type": ErrorType.GUARD_POLICY_BLOCK.value,
            }
        }
    if result.outcome is GuardOutcome.PARSE_ERROR:
        max_attempts = ctx.services.settings.agent.max_attempts_per_step
        return {
            "turn": fail_attempt(
                turn,
                ErrorType.GUARD_PARSE_ERROR,
                result.model_feedback or "Write a simpler query.",
                max_attempts,
            )
        }
    return {"turn": {**turn, "next": "check_sql"}}
