"""`schema_answer`: questions about the data and about the user's access (spec 31, B2.8).

No query runs.

- Access questions ("What can I check?", "Which brands do I have?", "What projects can I see?")
  get an answer that code writes from the `UserContext`: the role, the brands, the tables in plain
  words, the date range and three example questions. No model call. It never mentions cloud
  projects, keys or the system setup.
- Other schema questions go to the model with the schema without PII and a plain overview.
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from opsfleet_agent.agent.nodes.common import (
    brands_text,
    end_turn,
    is_manager,
    node,
    turn_of,
    user_format,
)
from opsfleet_agent.agent.nodes.replies import first_name, role_text
from opsfleet_agent.agent.periods import Calendar, format_day
from opsfleet_agent.agent.state import AgentState, TurnContext

LISTED_COLUMNS = (
    ("orders", "status"),
    ("order_items", "status"),
    ("products", "department"),
    ("products", "category"),
    ("users", "traffic_source"),
    ("users", "country"),
)
MAX_LISTED_VALUES = 30


def access_answer(ctx: TurnContext) -> str:
    services = ctx.services
    lib = services.prompts
    user = ctx.user
    manager = is_manager(user)
    first = services.values.first_date("order_items")
    if manager:
        brands = brands_text(user.brands)
        key = "manager" if len(user.brands) > 1 else "manager_single"
    else:
        brands = f"all {len(services.values.brands):,} brands"
        key = "ceo"
    examples = [
        lib.render_string(
            q, first_brand=user.brands[0] if user.brands else "", brands=brands
        )
        for q in lib.reply_value(f"examples.{key}")
    ]
    return lib.render(
        "access_answer",
        name=first_name(user),
        role_text=role_text(lib, user),
        brands_text=brands,
        first_date=format_day(first) if first else "the first order",
        today=format_day(Calendar.now().today),
        overview=lib.text("schema_overview").strip(),
        manager=manager,
        examples=examples,
    )


def fields_text(ctx: TurnContext) -> str:
    """The visible columns by table, without the project and dataset names."""
    schema = ctx.services.schema
    manager = is_manager(ctx.user)
    return "\n".join(
        f"{table}: {', '.join(schema.visible_columns(table, manager))}"
        for table in schema.tables
    )


def _values_text(ctx: TurnContext) -> str:
    values = ctx.services.values
    lines = []
    for table, column in LISTED_COLUMNS:
        found = values.get(table, column)
        if found:
            lines.append(f"{table}.{column}: {', '.join(found[:MAX_LISTED_VALUES])}")
    if is_manager(ctx.user):
        lines.append(f"brands of this user: {', '.join(ctx.user.brands)}")
    else:
        lines.append(f"brands: {len(values.brands):,} brands")
    return "\n".join(lines)


def model_schema_answer(state: AgentState, ctx: TurnContext) -> str:
    services = ctx.services
    lib = services.prompts
    user = ctx.user
    turn = turn_of(state)
    first = services.values.first_date("order_items")
    scope_key = "manager" if is_manager(user) else "ceo"
    system = lib.system(
        "schema_answer",
        persona=ctx.scratch.get("persona", ""),
        user_format=lib.format_text(user_format(state, turn)),
        scope_rule=lib.reply(
            f"scope_rule.{scope_key}", brands=brands_text(user.brands)
        ),
    )
    contents = "\n\n".join(
        [
            f"<overview>\n{lib.text('schema_overview').strip()}\n</overview>",
            f"<fields>\n{fields_text(ctx)}\n</fields>",
            f"<values>\n{_values_text(ctx)}\n</values>",
            (
                f"Data range: {format_day(first) if first else 'unknown'} to today, "
                f"{format_day(Calendar.now().today)}."
            ),
            f"<user_message>\n{turn['question']}\n</user_message>",
        ]
    )
    result = services.llm.generate(
        ctx.budget,
        system=system,
        prompt=contents,
        purpose="schema_answer",
        prompt_version=lib.version("safety", "schema_answer", "schema_overview"),
    )
    return result.text.strip()


@node("schema_answer")
def schema_answer(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    ctx = runtime.context
    turn = turn_of(state)
    if (turn.get("route") or {}).get("schema_kind") == "access":
        return end_turn(turn, access_answer(ctx))
    return end_turn(turn, model_schema_answer(state, ctx))
