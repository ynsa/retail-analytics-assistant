"""The answer footer, written by code from the turn data (spec 31, B2.5 rule 5 and B2.9 rule 4).

Example:
`Period: 1–31 Oct 2026 · Brands: Calvin Klein, Carhartt · Revenue = sale price of items that were
not cancelled or returned.` and, when the model used a default, `Assumed: last month = …`.
"""

from __future__ import annotations

from datetime import date

from opsfleet_agent.agent.context import PromptLibrary
from opsfleet_agent.agent.nodes.common import is_manager
from opsfleet_agent.agent.periods import format_day, parse_periods, span_of
from opsfleet_agent.agent.state import TurnState
from opsfleet_agent.identity import UserContext

# Period words: the footer shows the period itself, so these keys need no sentence.
PERIOD_KEYS = frozenset({"last_month", "quarter", "today", "sale_date"})
MAX_DEFINITIONS = 4
MAX_ASSUMPTIONS = 3
MAX_ASSUMPTION_CHARS = 80


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
    )[:MAX_ASSUMPTIONS]
    return period_text, brands, sentences, assumptions


def build_footer(
    turn: TurnState, user: UserContext, lib: PromptLibrary, today: date
) -> str:
    period_text, brands, sentences, assumptions = footer_parts(turn, user, lib, today)
    parts = [f"Period: {period_text}", f"Brands: {', '.join(brands)}"]
    parts += [s.rstrip(". ") for s in sentences]
    lines = [f"_{' · '.join(parts)}._"]
    if assumptions:
        lines.append(f"_Assumed: {'; '.join(assumptions)}._")
    return "\n\n".join(lines)
