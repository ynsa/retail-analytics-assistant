"""Periods that a question names with numbers, and the `<named_periods>` block of `write_sql`.

The Docker check of 2026-11-12: for "How many orders were placed in 2024?" the model copied the
`last_year` line of the calendar (2025). The block gives the dates of 2024 near the question.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from opsfleet_agent.agent.context import PromptLibrary
from opsfleet_agent.agent.nodes.write_sql import calendar_text, named_periods_block
from opsfleet_agent.agent.periods import Calendar
from opsfleet_agent.agent.question_periods import named_periods
from tests.conftest import FakeBQ, FakeLLM, T, frame_result, route_reply, sql_reply

TODAY = date(2026, 11, 12)


def labels(question: str) -> list[tuple[str, str, bool]]:
    return [
        (p.label, f"{p.period.start}..{p.period.end}", p.bound)
        for p in named_periods(question, TODAY)
    ]


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (
            "How many orders were placed in 2024?",
            [("2024", "2024-01-01..2024-12-31", False)],
        ),
        (
            "Revenue in Q3 2025 vs Q2 2025",
            [
                ("Q3 2025", "2025-07-01..2025-09-30", False),
                ("Q2 2025", "2025-04-01..2025-06-30", False),
            ],
        ),
        ("2025 Q4 orders", [("2025 Q4", "2025-10-01..2025-12-31", False)]),
        ("sales in October 2025", [("October 2025", "2025-10-01..2025-10-31", False)]),
        ("May 2024 revenue", [("May 2024", "2024-05-01..2024-05-31", False)]),
        ("revenue of 2024-02", [("2024-02", "2024-02-01..2024-02-29", False)]),
        ("H1 2025", [("H1 2025", "2025-01-01..2025-06-30", False)]),
        (
            "orders before 2020 and in 2024",
            [
                ("2020", "2020-01-01..2020-12-31", True),
                ("2024", "2024-01-01..2024-12-31", False),
            ],
        ),
        ("How many orders since 2023?", [("2023", "2023-01-01..2023-12-31", True)]),
    ],
)
def test_named_periods(question, expected):
    assert labels(question) == expected


@pytest.mark.parametrize(
    "question",
    [
        "Show the top 2000 customers by spend",
        "Which products sold 2020 units?",
        "Revenue on 2025-01-03",  # a full date is exact already
        "Orders in 2027",  # after today's year
        "How many orders were placed last year?",
        "",
    ],
)
def test_no_named_period(question):
    assert named_periods(question, TODAY) == []


def test_block_gives_the_dates_and_the_sql_of_a_named_year():
    block = named_periods_block("How many orders were placed in 2024?", TODAY)
    assert block == (
        "<named_periods>\n"
        "2024: 2024-01-01..2024-12-31 (1 Jan – 31 Dec 2024); SQL: created_at >= "
        "TIMESTAMP('2024-01-01') AND created_at < TIMESTAMP('2025-01-01')\n"
        "</named_periods>"
    )
    assert named_periods_block("orders last month", TODAY) == ""
    assert named_periods_block("customers who joined before 2020", TODAY) == ""


def test_calendar_marks_the_relative_periods():
    text = calendar_text(Calendar(TODAY))
    assert "Relative periods (use them only for these words in the question):" in text
    assert "last_year: 2025-01-01..2025-12-31" in text


def test_write_sql_rules_name_a_year_and_the_revenue_filter():
    prompt = PromptLibrary().text("write_sql")
    assert '"in 2024" is 2024-01-01..2024-12-31' in prompt
    assert "never for a named year" in prompt
    assert (
        "Revenue, orders, items sold, average order value and gross margin use the revenue "
        "filter `oi.status NOT IN ('Cancelled', 'Returned')`" in prompt
    )


def test_write_sql_prompt_has_the_named_year_near_the_question(make_session):
    sql = (
        f"SELECT COUNT(DISTINCT oi.order_id) AS orders FROM {T('order_items')} AS oi "
        "WHERE oi.created_at >= TIMESTAMP('2024-01-01') "
        "AND oi.created_at < TIMESTAMP('2025-01-01') "
        "AND oi.status NOT IN ('Cancelled', 'Returned')"
    )
    llm = FakeLLM(
        [
            route_reply(),
            sql_reply(sql, period="2024-01-01..2024-12-31"),
            "20,671 orders.",
        ]
    )
    bq = FakeBQ([frame_result(pd.DataFrame({"orders": [20671]}))])
    session, _, _ = make_session("ceo", llm=llm, bq=bq)
    session.ask("How many orders were placed in 2024?")
    prompt = llm.calls[1]["prompt"]
    block = prompt.split("<named_periods>")[1].split("</named_periods>")[0]
    assert "2024: 2024-01-01..2024-12-31" in block
    assert prompt.index("<calendar>") < prompt.index("<named_periods>")
    assert prompt.index("<named_periods>") < prompt.index("<user_message>")
