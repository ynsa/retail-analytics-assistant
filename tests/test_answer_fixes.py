"""Two agent fixes from the evaluation smoke run of 2026-11-12 (spec 23, B7a; spec 31, B7).

- PP-01: the model wrote a total that it calculated itself, and the figure check removed it. The
  prompts now ask for totals from SQL or from a `{{calc:...}}` placeholder. The check stays strict.
- TM-01: the footer said "Assumed: period = last_year" although the user named the year 2025. An
  assumption about the period now shows only when the question names no period.
"""

from __future__ import annotations

import pandas as pd
import pytest

from opsfleet_agent.agent.periods import states_period
from tests.conftest import FakeBQ, FakeLLM, frame_result, route_reply, sql_reply

T_ITEMS = "`bigquery-public-data.thelook_ecommerce.order_items`"
T_PRODUCTS = "`bigquery-public-data.thelook_ecommerce.products`"
BRAND_SQL = (
    f"SELECT p.brand, SUM(oi.sale_price) AS revenue FROM {T_ITEMS} oi "
    f"JOIN {T_PRODUCTS} p ON p.id = oi.product_id "
    "WHERE oi.status NOT IN ('Cancelled', 'Returned') "
    "AND oi.created_at >= TIMESTAMP('2026-10-01') AND oi.created_at < TIMESTAMP('2026-11-01') "
    "GROUP BY p.brand"
)
BRAND_TOTAL_SQL = (
    f"SELECT p.brand, SUM(oi.sale_price) AS revenue, "
    "SUM(SUM(oi.sale_price)) OVER () AS total_revenue "
    f"FROM {T_ITEMS} oi JOIN {T_PRODUCTS} p ON p.id = oi.product_id "
    "WHERE oi.status NOT IN ('Cancelled', 'Returned') "
    "AND oi.created_at >= TIMESTAMP('2026-10-01') AND oi.created_at < TIMESTAMP('2026-11-01') "
    "GROUP BY p.brand"
)
REMOVED = "[figure removed: not traceable]"


def brands(total: bool = False):
    data = {"brand": ["Calvin Klein", "Carhartt"], "revenue": [2000.5, 1400.25]}
    if total:
        data["total_revenue"] = [3400.75, 3400.75]
    return frame_result(pd.DataFrame(data))


def ask(make_session, sql: str, answer: str, frame, question: str):
    llm = FakeLLM([route_reply(), sql_reply(sql, definitions=["revenue"]), answer])
    session, llm, bq = make_session("maya", llm=llm, bq=FakeBQ([frame]))
    return session.ask(question), llm, bq


# --- PP-01: totals -------------------------------------------------------------------------------


def test_total_from_a_placeholder_is_computed_and_kept(make_session):
    result, _, _ = ask(
        make_session,
        BRAND_SQL,
        "Your brands had a total revenue of {{calc:sum(r1.revenue)}}. Calvin Klein had "
        "$2,000.50 and Carhartt had $1,400.25.",
        brands(),
        "What was the revenue of each of my brands last month?",
    )
    assert "a total revenue of $3,400.75." in result.answer
    assert REMOVED not in result.answer and result.unverified_figures == 0


def test_total_from_sql_is_traceable(make_session):
    result, _, bq = ask(
        make_session,
        BRAND_TOTAL_SQL,
        "Your brands had a total revenue of $3,400.75. Calvin Klein had $2,000.50.",
        brands(total=True),
        "What was the revenue of each of my brands last month?",
    )
    assert (
        bq.queries and "OVER ()" in bq.queries[0]
    )  # the guard passed the window total
    assert "$3,400.75" in result.answer and result.unverified_figures == 0


def test_a_total_that_the_model_calculated_is_still_removed(make_session):
    """The figure check stays strict: a sum that is not in the data is not traceable."""
    result, _, _ = ask(
        make_session,
        BRAND_SQL,
        "Your brands had a total revenue of $3,400.75. Calvin Klein had $2,000.50.",
        brands(),
        "What was the revenue of each of my brands last month?",
    )
    assert f"a total revenue of {REMOVED}" in result.answer
    assert result.unverified_figures == 1


def test_prompts_ask_for_totals_from_sql_or_a_placeholder(make_session):
    _, llm, _ = ask(
        make_session,
        BRAND_SQL,
        "Calvin Klein had $2,000.50.",
        brands(),
        "What was the revenue of each of my brands last month?",
    )
    sql_system = next(c["system"] for c in llm.calls if c["purpose"] == "write_sql")
    answer_system = next(
        c["system"] for c in llm.calls if c["purpose"] == "write_answer"
    )
    assert "also return the total of all groups" in sql_system
    assert "SUM(SUM(oi.sale_price)) OVER () AS all_groups_total_revenue" in sql_system
    assert "{{calc:sum(r1.revenue)}}" in answer_system
    assert "no totals, sums, averages" in answer_system


# --- TM-01: assumptions in the footer ------------------------------------------------------------


def test_no_period_assumption_when_the_user_named_the_year(make_session):
    llm = FakeLLM(
        [
            route_reply(),
            sql_reply(
                BRAND_SQL,
                period="2025-01-01..2025-12-31",
                definitions=["revenue", "last_year"],
                assumptions=["period = last_year", "brands = all brands of the user"],
            ),
            "Your brands had $2,000.50 and $1,400.25.",
        ]
    )
    session, _, _ = make_session("maya", llm=llm, bq=FakeBQ([brands()]))
    result = session.ask("What was the revenue of my brands in 2025?")
    assert "Period: 1 Jan – 31 Dec 2025" in result.footer
    assert "period = last_year" not in result.footer
    assert "_Assumed: brands = all brands of the user._" in result.footer


def test_period_assumption_stays_when_the_period_is_a_default(make_session):
    llm = FakeLLM(
        [
            route_reply(),
            sql_reply(
                BRAND_SQL,
                assumptions=[
                    "last month = 1–31 Oct 2026",
                    "figures = default overview",
                ],
            ),
            "Your brands had $2,000.50 and $1,400.25.",
        ]
    )
    session, _, _ = make_session("maya", llm=llm, bq=FakeBQ([brands()]))
    result = session.ask("How are my brands doing?")
    assert (
        "_Assumed: last month = 1–31 Oct 2026; figures = default overview._"
        in result.footer
    )


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("What was the revenue of my brands in 2025?", True),
        ("Compare Q1 and Q2", True),
        ("Revenue in March", True),
        ("Revenue in May", True),
        ("the stats for the last nmonth", True),
        ("Sales this year so far", True),
        ("Orders between 2026-01-01 and 2026-02-01", True),
        ("Revenue in the first half of 2026", True),
        ("How are my brands doing?", False),
        ("Which brands may I see?", False),
        ("Top 5 categories by revenue", False),
        ("", False),
    ],
)
def test_states_period(question, expected):
    assert states_period(question) is expected


# --- Spec 50 round 1: SQL prompt rules, glossary and data markup -------------------------------


def test_sql_prompts_have_the_round_1_rules(make_session):
    _, llm, _ = ask(
        make_session,
        BRAND_SQL,
        "Calvin Klein had $2,000.50.",
        brands(),
        "What was the revenue of each of my brands last month?",
    )
    call = next(c for c in llm.calls if c["purpose"] == "write_sql")
    system, prompt = call["system"], call["prompt"]
    assert (
        'Write a text with an apostrophe in double quotes, for example `p.brand = "Levi\'s"`'
        in system
    )
    assert (
        '"Calvin Klein Jeans" is a brand, not "Calvin Klein" with the category'
        in system
    )
    assert "When the user names all brands of the scope" in system
    assert "A count of customers uses the purchase filter" in system
    assert "Filter and group by the same date column." in system
    assert "also read the period before the first one" in system
    assert "### repeat_customer" in prompt  # the glossary in the context
    assert "LEAD(d) OVER (PARTITION BY user_id ORDER BY d)" in prompt
    answer_system = next(
        c["system"] for c in llm.calls if c["purpose"] == "write_answer"
    )
    assert "`all_groups_total_`" in answer_system


def test_copied_data_block_is_removed_from_the_answer(make_session):
    """TM-10: the answer showed the internal `<data>` block."""
    result, _, _ = ask(
        make_session,
        BRAND_SQL,
        'Calvin Klein had $2,000.50.\n\n<data source="query" ref="r1" rows="2">\n'
        "| brand | revenue |\n| Carhartt | $1,400.25 |\n</data>\n\nThe data shows growth.",
        brands(),
        "What was the revenue of each of my brands last month?",
    )
    assert "<data" not in result.answer and "</data>" not in result.answer
    assert "Calvin Klein had $2,000.50." in result.answer
    assert "The data shows growth." in result.answer


def test_strip_data_markup_keeps_normal_text():
    from opsfleet_agent.agent.formatting import strip_data_markup

    assert strip_data_markup("A <data ref='r1'/> B </data> C") == "A  B  C"
    assert strip_data_markup("No markup, 12.5%.") == "No markup, 12.5%."
