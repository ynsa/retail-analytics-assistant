"""The answer footer, written by code from the turn data (spec 31, B2.5 rule 5 and B2.9 rule 4).

Example:
`Period: 1–31 Oct 2026 · Brands: Calvin Klein, Carhartt · Revenue = sale price of items that were
not cancelled or returned.` and, when the model used a default, `Assumed: last month = …`.

For a manager, when a query that ran reads the `users` table, the footer says that user and
customer figures cover only the customers who bought the manager's brands (spec 50, round 1, XB-06:
"661 new users" without this note). Code adds the note, because the model did not follow the
scope phrase of the prompt.

An assumption shows only when a default really applied. When the question names a period, an
assumption about the period is not a default, so the footer leaves it out (smoke run TM-01: "the
revenue in 2025" gave "Assumed: period = last_year").
"""

from __future__ import annotations

import re
from datetime import date

from opsfleet_agent.agent.context import PromptLibrary
from opsfleet_agent.agent.nodes.common import is_manager
from opsfleet_agent.agent.periods import (
    format_day,
    parse_periods,
    span_of,
    states_period,
)
from opsfleet_agent.agent.state import TurnState
from opsfleet_agent.identity import UserContext

# Period words: the footer shows the period itself, so these keys need no sentence.
PERIOD_KEYS = frozenset({"last_month", "quarter", "today", "sale_date"})
MAX_DEFINITIONS = 4
MAX_ASSUMPTIONS = 3
MAX_ASSUMPTION_CHARS = 80
# An assumption with one of these words is about the period.
_PERIOD_WORDS = re.compile(
    r"\b(?:period|date|dates|day|week|month|year|quarter|q[1-4])\b|last_|this_|\d{4}",
    re.IGNORECASE,
)


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(i for i in items if i))


def footer_parts(
    turn: TurnState, user: UserContext, lib: PromptLibrary, today: date
) -> tuple[str, list[str], list[str], list[str]]:
    """The period text, the brands, the definition sentences and the assumptions."""
    plans = turn.get("plans") or []
    periods = []
    for plan in plans:
        periods += parse_periods((plan.get("active_filters") or {}).get("period"))
    period = span_of(periods)
    period_text = (
        period.text()
        if period
        else lib.reply("footer.no_period", today=format_day(today))
    )

    brands = _unique(
        [b for step in turn.get("steps") or [] for b in step.get("brands") or []]
    )
    if not brands:
        brands = (
            list(user.brands) if is_manager(user) else [lib.reply("footer.all_brands")]
        )

    keys = _unique([k for plan in plans for k in plan.get("definitions_used") or []])
    sentences = [
        lib.definitions[k].footer
        for k in keys
        if k in lib.definitions and k not in PERIOD_KEYS and lib.definitions[k].footer
    ][:MAX_DEFINITIONS]

    assumptions = _unique(
        [
            a.strip().rstrip(".")[:MAX_ASSUMPTION_CHARS]
            for plan in plans
            for a in plan.get("assumptions") or []
        ]
    )
    if states_period(turn.get("question")):
        assumptions = [a for a in assumptions if not is_period_assumption(a)]
    return period_text, brands, sentences, assumptions[:MAX_ASSUMPTIONS]


USERS_TABLE = "users"


def users_scope_note(turn: TurnState, user: UserContext, lib: PromptLibrary) -> str:
    """The scope note of a manager's answer that used the `users` table, or ""."""
    if not is_manager(user):
        return ""
    ran = [s for s in turn.get("steps") or [] if "row_count" in s]
    if not any(USERS_TABLE in (s.get("tables") or []) for s in ran):
        return ""
    return lib.reply("footer.users_scope", brands=", ".join(user.brands))


def is_period_assumption(text: str) -> bool:
    """`period = last_year`, `last month = 1–31 Oct 2026`, `dates = 2025-01-01..2025-12-31`."""
    return bool(_PERIOD_WORDS.search(text))


def build_footer(
    turn: TurnState, user: UserContext, lib: PromptLibrary, today: date
) -> str:
    period_text, brands, sentences, assumptions = footer_parts(turn, user, lib, today)
    parts = [f"Period: {period_text}", f"Brands: {', '.join(brands)}"]
    if note := users_scope_note(turn, user, lib):
        parts.append(note.rstrip(". "))
    parts += [s.rstrip(". ") for s in sentences]
    lines = [f"_{' · '.join(parts)}._"]
    if assumptions:
        lines.append(f"_Assumed: {'; '.join(assumptions)}._")
    return "\n\n".join(lines)
