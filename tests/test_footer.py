"""The answer footer, written by code from the turn data (spec 31, B2.5 rule 5 and B2.9 rule 4)."""

from __future__ import annotations

from datetime import date

from opsfleet_agent.agent.context import PromptLibrary
from opsfleet_agent.agent.footer import build_footer
from tests.conftest import CEO, MANAGER


def test_footer_with_period_brands_definitions_and_assumptions():
    lib = PromptLibrary()
    turn = {
        "plans": [
            {
                "active_filters": {"period": "2026-10-01..2026-10-31"},
                "definitions_used": ["revenue", "last_month", "orders", "unknown_key"],
                "assumptions": [
                    "last month = 1–31 Oct 2026",
                    "figures = default overview.",
                ],
            }
        ],
        "steps": [{"brands": []}],
    }
    footer = build_footer(turn, MANAGER, lib, date(2026, 11, 11))
    assert footer == (
        "_Period: 1–31 Oct 2026 · Brands: Calvin Klein, Levi's · Revenue = sale price of items "
        "that were not cancelled or returned · Orders = orders with at least one item that was "
        "not cancelled or returned._\n\n"
        "_Assumed: last month = 1–31 Oct 2026; figures = default overview._"
    )


def test_footer_uses_the_brand_filter_of_the_guard_and_all_brands_for_the_ceo():
    lib = PromptLibrary()
    turn = {"plans": [{"active_filters": {}}], "steps": [{"brands": ["Nike"]}]}
    assert "Brands: Nike" in build_footer(turn, CEO, lib, date(2026, 11, 11))
    turn = {"plans": [{"active_filters": {}}], "steps": []}
    footer = build_footer(turn, CEO, lib, date(2026, 11, 11))
    assert footer == "_Period: all data up to 11 Nov 2026 · Brands: all brands._"


def test_manager_answer_from_the_users_table_says_the_scope_of_the_figures():
    """Spec 50, round 1, XB-06: "661 new users" must say that only the customers of the
    manager's brands count. Code adds the note, not the prompt."""
    lib = PromptLibrary()
    ran = {"tables": ["order_items", "products", "users"], "row_count": 1}
    turn = {"plans": [{"active_filters": {}}], "steps": [ran]}
    footer = build_footer(turn, MANAGER, lib, date(2026, 11, 11))
    assert footer == (
        "_Period: all data up to 11 Nov 2026 · Brands: Calvin Klein, Levi's · User and customer "
        "figures cover only the customers who bought Calvin Klein, Levi's._"
    )
    # The CEO sees all customers: no note.
    assert "customers who bought" not in build_footer(
        turn, CEO, lib, date(2026, 11, 11)
    )
    # No note for a query without `users`, or for a `users` query that did not run.
    other = {"tables": ["order_items", "products"], "row_count": 3}
    failed = {"tables": ["users"]}
    for steps in ([other], [failed]):
        turn = {"plans": [{"active_filters": {}}], "steps": steps}
        assert "customers who bought" not in build_footer(
            turn, MANAGER, lib, date(2026, 11, 11)
        )
