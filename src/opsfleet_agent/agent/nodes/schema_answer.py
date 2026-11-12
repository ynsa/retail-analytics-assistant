"""`schema_answer`: questions about the data and about the user's access (spec 31, B2.8).

No query runs.

- Access questions ("What can I check?", "Which brands do I have?", "What projects can I see?")
  get an answer that code writes from the `UserContext`: the role, the brands, the tables in plain
  words, the date range and three example questions. No model call. It never mentions cloud
  projects, keys or the system setup.
- Other schema questions go to the model with the schema without PII, a plain overview and the
  footer sentences of the glossary (spec 50 round 1: "How do you calculate revenue?").
- The sentence about the missing customer fields (names, emails, street addresses) is for a
  question about customers, about personal data or about all the data. For other questions,
  code gives the model the overview without that sentence and removes such a sentence from the
  answer (spec 50 round 1: "How do you calculate revenue?" got it too).
- Questions about this conversation (`schema_kind = conversation`, spec 34) go to the model with
  the pinned facts, the summary and the recent turns.
"""

from __future__ import annotations

import re
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
from opsfleet_agent.agent.personal_data import asks_personal_data
from opsfleet_agent.agent.state import AgentState, TurnContext
from opsfleet_agent.memory.context_builder import builder_of

LISTED_COLUMNS = (
    ("orders", "status"),
    ("order_items", "status"),
    ("products", "department"),
    ("products", "category"),
    ("users", "traffic_source"),
    ("users", "country"),
)
MAX_LISTED_VALUES = 30

# A question about people: customers, users, sign-ups, demographic fields.
_PEOPLE = re.compile(
    r"\b(?:customers?|users?|people|persons?|buyers?|shoppers?|clients?|demographics?"
    r"|sign[- ]?ups?|signed\s+up|ages?|genders?|personal)\b",
    re.IGNORECASE,
)
# A question about all the data: "What data do you have?", "Which tables can I use?".
_ALL_DATA = re.compile(
    r"\b(?:what|which)\s+(?:kinds?\s+of\s+)?(?:data|tables|information|fields|datasets)\b",
    re.IGNORECASE,
)
# A sentence that says that a personal field is not in the data.
_PERSONAL_FIELD = re.compile(
    r"\b(?:names?|e-?mails?|e-?mail\s+address(?:es)?|street\s+address(?:es)?|addresses"
    r"|personal\s+data)\b",
    re.IGNORECASE,
)
_NOT_IN_DATA = re.compile(
    r"\b(?:not\s+available|unavailable|not\s+in\s+the\s+data|not\s+included|missing"
    r"|(?:does|do)\s+not\s+(?:include|have|contain))\b",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_BULLET = re.compile(r"^\s*(?:[-*]|\d+\.)?\s*$")


def about_people(question: str) -> bool:
    """True when the missing customer fields belong in the answer: a question about customers
    or people, about personal data, or about all the data."""
    q = question or ""
    return bool(_PEOPLE.search(q) or _ALL_DATA.search(q) or asks_personal_data(q))


def without_missing_fields(answer: str) -> str:
    """The answer without the sentences that say that personal fields are not in the data."""
    lines = []
    for line in answer.splitlines():
        kept = [
            s
            for s in _SENTENCE_END.split(line)
            if not (_PERSONAL_FIELD.search(s) and _NOT_IN_DATA.search(s))
        ]
        text = " ".join(kept)
        if line.strip() and _BULLET.match(text):
            continue  # the line had only that sentence
        lines.append(text)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def overview_for(lib: Any, people: bool) -> str:
    """The schema overview. Without the people part, the sentence about missing fields goes."""
    overview = lib.text("schema_overview").strip()
    return overview if people else without_missing_fields(overview)


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


def definitions_text(ctx: TurnContext) -> str:
    """The footer sentence of each glossary entry (spec 31 note 12: a schema answer can quote the
    definitions). The glossary is not a protected prompt."""
    return "\n".join(
        f"- {d.key}: {d.footer or d.meaning}"
        for d in ctx.services.prompts.definitions.values()
    )


def model_schema_answer(state: AgentState, ctx: TurnContext) -> str:
    services = ctx.services
    lib = services.prompts
    user = ctx.user
    turn = turn_of(state)
    first = services.values.first_date("order_items")
    scope_key = "manager" if is_manager(user) else "ceo"
    people = about_people(turn["question"])
    system = lib.system(
        "schema_answer",
        persona=ctx.scratch.get("persona", ""),
        user_format=lib.format_text(user_format(state, turn)),
        scope_rule=lib.reply(
            f"scope_rule.{scope_key}", brands=brands_text(user.brands)
        ),
        about_people=people,
    )
    request = builder_of(services).build(
        "schema_answer",
        system=system,
        state=state,
        stable=[
            f"<overview>\n{overview_for(lib, people)}\n</overview>",
            f"<fields>\n{fields_text(ctx)}\n</fields>",
            f"<values>\n{_values_text(ctx)}\n</values>",
            f"<definitions>\n{definitions_text(ctx)}\n</definitions>",
            (
                f"Data range: {format_day(first) if first else 'unknown'} to today, "
                f"{format_day(Calendar.now().today)}."
            ),
        ],
        user_message=turn["question"],
    )
    result = services.llm.generate(
        ctx.budget,
        system=request.system,
        prompt=request.prompt,
        purpose="schema_answer",
        prompt_version=lib.version(
            "safety", "schema_answer", "schema_overview", "definitions"
        ),
    )
    answer = result.text.strip()
    return answer if people else without_missing_fields(answer) or answer


def conversation_answer(state: AgentState, ctx: TurnContext) -> str:
    """A question about this conversation (spec 34, B7): answered from the pinned facts, the
    summary and the recent turns. No query runs."""
    services = ctx.services
    lib = services.prompts
    turn = turn_of(state)
    system = lib.system(
        "conversation_answer",
        persona=ctx.scratch.get("persona", ""),
        user_format=lib.format_text(user_format(state, turn)),
    )
    request = builder_of(services).build(
        "conversation_answer", system=system, state=state, user_message=turn["question"]
    )
    result = services.llm.generate(
        ctx.budget,
        system=request.system,
        prompt=request.prompt,
        purpose="conversation_answer",
        prompt_version=lib.version("safety", "conversation_answer"),
    )
    return result.text.strip()


@node("schema_answer")
def schema_answer(state: AgentState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    ctx = runtime.context
    turn = turn_of(state)
    kind = (turn.get("route") or {}).get("schema_kind")
    if kind == "access":
        return end_turn(turn, access_answer(ctx))
    if kind == "conversation":
        return end_turn(turn, conversation_answer(state, ctx))
    return end_turn(turn, model_schema_answer(state, ctx))
