"""The period and definition checks before a query runs (spec 31, B7).

The Docker check of 2026-11-12: "How many orders were placed in 2024?" ran with 2025 dates (and once
without the status filter), and the footer still said "Period: 1 Jan – 31 Dec 2024" and cited the
orders definition. The checks compare the model's SQL with the stated period and the cited
definitions, send feedback to the model, and keep the footer true.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from opsfleet_agent.agent.context import PromptLibrary
from opsfleet_agent.agent.sql_checks.consistency import StepCheck, check_step
from opsfleet_agent.agent.sql_checks.date_filters import read_date_filters
from opsfleet_agent.agent.sql_checks.ranges import interval_text
from opsfleet_agent.agent.sql_checks.status_filters import read_status_filters
from opsfleet_agent.guard.sql_guard import SqlGuard
from tests.conftest import (
    CEO,
    MANAGER,
    FakeBQ,
    FakeLLM,
    T,
    frame_result,
    load_test_schema,
    load_test_values,
    read_spans,
    route_reply,
    sql_reply,
)

TODAY = date(2026, 11, 12)
FIRST = date(2019, 1, 6)
OI = T("order_items")
P = T("products")
Y2024 = "2024-01-01..2024-12-31"
Y2025 = "2025-01-01..2025-12-31"
REVENUE_FILTER = "oi.status NOT IN ('Cancelled', 'Returned')"
QUESTION = "How many orders were placed in 2024?"


def orders_sql(start: str, end: str, status: bool = True) -> str:
    sql = (
        f"SELECT COUNT(DISTINCT oi.order_id) AS orders FROM {OI} AS oi "
        f"WHERE oi.created_at >= TIMESTAMP('{start}') AND oi.created_at < TIMESTAMP('{end}')"
    )
    return f"{sql} AND {REVENUE_FILTER}" if status else sql


SQL_2024 = orders_sql("2024-01-01", "2025-01-01")
SQL_2025 = orders_sql("2025-01-01", "2026-01-01")  # trace 2e618057
SQL_2025_NO_STATUS = orders_sql("2025-01-01", "2026-01-01", status=False)  # 6b5267df
SQL_2024_NO_STATUS = orders_sql("2024-01-01", "2025-01-01", status=False)


def plan(period: str | None = Y2024, definitions=("orders",), other=()) -> dict:
    return {
        "definitions_used": list(definitions),
        "active_filters": {"period": period, "brands": [], "other": list(other)},
    }


def run_check(
    sql: str,
    the_plan: dict | None = None,
    question: str = QUESTION,
    user=CEO,
) -> StepCheck:
    guard = SqlGuard(user, load_test_schema(), load_test_values())
    result = guard.check(sql)
    assert result.passed, result.model_feedback or result.user_message
    return check_step(
        the_plan if the_plan is not None else plan(),
        question,
        result,
        load_test_schema(),
        PromptLibrary(),
        TODAY,
        FIRST,
    )


def ranges(sql: str, user=CEO) -> list[str]:
    result = SqlGuard(user, load_test_schema(), load_test_values()).check(sql)
    filters = read_date_filters(result.qualified_sql or "", load_test_schema())
    assert filters is not None
    column = filters.columns[("order_items", "created_at")]
    assert not column.unknown
    return [interval_text(i) for i in column.known()]


# --- reading the date filters ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        (
            "oi.created_at >= TIMESTAMP('2024-01-01') AND oi.created_at < TIMESTAMP('2025-01-01')",
            ["2024-01-01 to 2025-01-01"],
        ),
        ("EXTRACT(YEAR FROM oi.created_at) = 2024", ["2024-01-01 to 2025-01-01"]),
        (
            "EXTRACT(YEAR FROM oi.created_at) BETWEEN 2023 AND 2024",
            ["2023-01-01 to 2025-01-01"],
        ),
        (
            "EXTRACT(YEAR FROM oi.created_at) = 2024 AND EXTRACT(QUARTER FROM oi.created_at) = 3",
            ["2024-07-01 to 2024-10-01"],
        ),
        (
            "DATE(oi.created_at) BETWEEN '2024-01-01' AND '2024-12-31'",
            ["2024-01-01 to 2025-01-01"],
        ),
        (
            (
                "CAST(oi.created_at AS DATE) >= DATE '2024-01-01' "
                "AND CAST(oi.created_at AS DATE) <= DATE '2024-12-31'"
            ),
            ["2024-01-01 to 2025-01-01"],
        ),
        (
            "DATE_TRUNC(DATE(oi.created_at), YEAR) = DATE '2024-01-01'",
            ["2024-01-01 to 2025-01-01"],
        ),
        (
            (
                "TIMESTAMP_TRUNC(oi.created_at, MONTH) "
                "BETWEEN TIMESTAMP('2024-01-01') AND TIMESTAMP('2024-12-01')"
            ),
            ["2024-01-01 to 2025-01-01"],
        ),
        (
            "FORMAT_DATE('%Y', DATE(oi.created_at)) = '2024'",
            ["2024-01-01 to 2025-01-01"],
        ),
        (
            "FORMAT_TIMESTAMP('%Y-%m', oi.created_at) = '2024-10'",
            ["2024-10-01 to 2024-11-01"],
        ),
        (
            "oi.created_at BETWEEN '2024-01-01' AND '2024-12-31'",
            ["2024-01-01 to 2024-12-31 00:00:00 (included)"],
        ),
        ("oi.created_at >= TIMESTAMP('2024-01-01')", ["2024-01-01 with no end date"]),
    ],
)
def test_date_filter_forms(condition, expected):
    sql = f"SELECT COUNT(*) AS n FROM {OI} AS oi WHERE {condition}"
    assert ranges(sql) == expected


def test_date_filter_through_a_cte_and_in_case_when():
    sql = (
        f"WITH x AS (SELECT DATE(oi.created_at) AS d, oi.sale_price FROM {OI} AS oi) "
        "SELECT SUM(sale_price) AS revenue FROM x WHERE d >= '2024-01-01' AND d < '2025-01-01'"
    )
    assert ranges(sql) == ["2024-01-01 to 2025-01-01"]
    case = (
        "SELECT SUM(CASE WHEN oi.created_at >= TIMESTAMP('2026-10-01') THEN oi.sale_price END) "
        f"AS revenue FROM {OI} AS oi WHERE oi.created_at >= TIMESTAMP('2026-09-01') "
        "AND oi.created_at < TIMESTAMP('2026-11-01')"
    )
    assert ranges(case) == ["2026-09-01 to 2026-11-01", "2026-10-01 with no end date"]


def test_two_ranges_with_or():
    sql = (
        f"SELECT COUNT(*) AS n FROM {OI} AS oi WHERE "
        "(oi.created_at >= '2024-01-01' AND oi.created_at < '2024-04-01') OR "
        "(oi.created_at >= '2025-01-01' AND oi.created_at < '2025-04-01')"
    )
    assert ranges(sql) == ["2024-01-01 to 2024-04-01", "2025-01-01 to 2025-04-01"]


@pytest.mark.parametrize(
    "condition",
    [
        "oi.created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)",
        "DATE_TRUNC(oi.created_at, WEEK) = '2024-01-01'",
        "NOT (oi.created_at >= '2024-01-01')",
        "EXTRACT(MONTH FROM oi.created_at) = 10",  # a month without a year
    ],
)
def test_unknown_forms_are_unverified_not_blocked(condition):
    sql = (
        f"SELECT COUNT(DISTINCT oi.order_id) AS orders FROM {OI} AS oi "
        f"WHERE {condition} AND {REVENUE_FILTER}"
    )
    check = run_check(sql)
    assert check.period.status == "unverified" and check.feedback == ""


def test_date_in_an_aggregate_is_not_a_period_filter():
    sql = (
        f"SELECT oi.user_id, MAX(oi.created_at) AS last_at FROM {OI} AS oi "
        "GROUP BY oi.user_id HAVING MAX(oi.created_at) < TIMESTAMP('2026-08-01')"
    )
    check = run_check(sql, plan(period=None, definitions=()), question="Churned?")
    assert check.period.status == "no_period" and check.feedback == ""


# --- the period rules -----------------------------------------------------------------------------


def test_wrong_year_gives_exact_feedback():
    check = run_check(SQL_2025)
    assert check.period.status == "mismatch" and check.period.code == "outside"
    assert check.feedback.startswith(
        "The period of the question is 1 Jan – 31 Dec 2024 (2024-01-01..2024-12-31), but the "
        "SQL filters order_items.created_at from 2025-01-01 to 2026-01-01."
    )
    assert (
        "oi.created_at >= TIMESTAMP('2024-01-01') AND oi.created_at < TIMESTAMP('2025-01-01')"
        in check.feedback
    )


def test_right_year_passes_and_gives_the_footer_period():
    check = run_check(SQL_2024)
    assert check.period.status == "match" and check.feedback == ""
    assert check.period_text == Y2024
    assert check.definitions.applied == ("orders",)


@pytest.mark.parametrize(
    "condition",
    [
        "EXTRACT(YEAR FROM oi.created_at) = 2024",
        "DATE(oi.created_at) BETWEEN '2024-01-01' AND '2024-12-31'",
        "DATE_TRUNC(DATE(oi.created_at), YEAR) = '2024-01-01'",
        "FORMAT_DATE('%Y', DATE(oi.created_at)) = '2024'",
    ],
)
def test_other_forms_of_2024_pass(condition):
    sql = (
        f"SELECT COUNT(DISTINCT oi.order_id) AS orders FROM {OI} AS oi "
        f"WHERE {condition} AND {REVENUE_FILTER}"
    )
    assert run_check(sql).period.status == "match"


@pytest.mark.parametrize(
    "condition",
    [
        "EXTRACT(YEAR FROM oi.created_at) = 2025",
        "DATE(oi.created_at) BETWEEN '2025-01-01' AND '2025-12-31'",
        "DATE_TRUNC(DATE(oi.created_at), YEAR) = '2025-01-01'",
    ],
)
def test_other_forms_of_the_wrong_year_are_found(condition):
    sql = (
        f"SELECT COUNT(DISTINCT oi.order_id) AS orders FROM {OI} AS oi "
        f"WHERE {condition} AND {REVENUE_FILTER}"
    )
    assert run_check(sql).period.code == "outside"


def test_timestamp_between_leaves_out_the_last_day():
    sql = (
        f"SELECT COUNT(DISTINCT oi.order_id) AS orders FROM {OI} AS oi "
        f"WHERE oi.created_at BETWEEN '2024-01-01' AND '2024-12-31' AND {REVENUE_FILTER}"
    )
    check = run_check(sql)
    assert check.period.code == "not_covered"
    assert "2024-01-01 to 2024-12-31 00:00:00 (included)" in check.feedback
    assert "leaves out the last day" in check.feedback


def test_an_earlier_year_is_found_by_the_coverage_rule():
    check = run_check(orders_sql("2023-01-01", "2024-01-01"))
    assert check.period.code == "not_covered"


def test_no_end_date_for_a_past_period():
    sql = (
        f"SELECT COUNT(DISTINCT oi.order_id) AS orders FROM {OI} AS oi "
        f"WHERE oi.created_at >= TIMESTAMP('2024-01-01') AND {REVENUE_FILTER}"
    )
    check = run_check(sql)
    assert check.period.code == "no_end"
    assert "oi.created_at < TIMESTAMP('2025-01-01')" in check.feedback.replace(
        "order_items.created_at", "oi.created_at"
    )


def test_no_date_filter_with_a_stated_period():
    sql = f"SELECT COUNT(DISTINCT oi.order_id) AS orders FROM {OI} AS oi WHERE {REVENUE_FILTER}"
    check = run_check(sql)
    assert check.period.code == "no_date_filter"
    assert "the SQL has no date filter" in check.feedback


def test_all_data_needs_no_date_filter():
    sql = f"SELECT COUNT(DISTINCT oi.order_id) AS orders FROM {OI} AS oi WHERE {REVENUE_FILTER}"
    check = run_check(sql, plan(period=f"2019-01-01..{TODAY}"), question="All orders?")
    assert check.feedback == ""


def test_this_year_so_far_with_the_today_limit():
    sql = orders_sql("2026-01-01", "2026-11-13")
    check = run_check(
        sql, plan(period="2026-01-01..2026-12-31"), question="Orders this year?"
    )
    assert check.period.status == "match"


def test_named_year_and_a_wrong_stated_period():
    """The model states 2025 and filters 2025 for a question about 2024."""
    check = run_check(SQL_2025, plan(period=Y2025))
    assert check.period.code == "question"
    assert check.feedback.startswith(
        "The question names 2024, but active_filters.period is 1 Jan – 31 Dec 2025"
    )
    assert "Do not use last_year for a named year." in check.feedback


def test_a_bound_year_is_not_the_asked_period():
    sql = orders_sql("2026-10-01", "2026-11-01")
    check = run_check(
        sql,
        plan(period="2026-10-01..2026-10-31"),
        question="Orders last month of customers who joined before 2020?",
    )
    assert check.period.status == "match"


def test_comparison_with_two_ranges():
    sql = (
        "SELECT EXTRACT(YEAR FROM oi.created_at) AS year, COUNT(DISTINCT oi.order_id) AS orders "
        f"FROM {OI} AS oi WHERE EXTRACT(YEAR FROM oi.created_at) IN (2024, 2025) "
        f"AND {REVENUE_FILTER} GROUP BY year"
    )
    stated = plan(period=Y2025, other=[f"compared with {Y2024}"])
    check = run_check(sql, stated, question="Orders in 2025 compared with 2024?")
    assert check.period.status == "match" and check.period_text == Y2025
    split = (
        "SELECT COUNTIF(oi.created_at >= TIMESTAMP('2025-01-01')) AS orders, "
        "COUNTIF(oi.created_at < TIMESTAMP('2025-01-01')) AS orders_prev "
        f"FROM {OI} AS oi WHERE oi.created_at >= TIMESTAMP('2024-01-01') "
        f"AND oi.created_at < TIMESTAMP('2026-01-01') AND {REVENUE_FILTER}"
    )
    assert run_check(split, stated, question="2025 vs 2024?").feedback == ""


def test_comparison_period_outside_the_stated_periods():
    sql = (
        f"SELECT COUNT(DISTINCT oi.order_id) AS orders FROM {OI} AS oi WHERE "
        "((oi.created_at >= '2025-01-01' AND oi.created_at < '2026-01-01') OR "
        "(oi.created_at >= '2022-01-01' AND oi.created_at < '2023-01-01')) "
        f"AND {REVENUE_FILTER}"
    )
    check = run_check(sql, plan(period=Y2025), question="Orders in 2025?")
    assert check.period.code == "outside"
    assert "from 2022-01-01 to 2023-01-01" in check.feedback
    assert "write it in active_filters.other" in check.feedback


def test_default_last_month_without_a_period_in_the_question():
    sql = orders_sql("2026-10-01", "2026-11-01")
    check = run_check(
        sql, plan(period="2026-10-01..2026-10-31"), question="How many orders?"
    )
    assert check.period.status == "match"
    assert check.period_text == "2026-10-01..2026-10-31"
    wrong = orders_sql("2026-09-01", "2026-10-01")
    assert (
        run_check(
            wrong, plan(period="2026-10-01..2026-10-31"), question="How many orders?"
        ).period.code
        == "not_covered"
    )


def test_no_stated_period_takes_the_period_of_the_sql():
    check = run_check(SQL_2024, plan(period=None), question="How many orders?")
    assert check.period.status == "match" and check.period.code == "from_sql"
    assert check.period_text == Y2024


def test_manager_sql_is_checked_before_the_scope_rewrite():
    sql = (
        f"SELECT COUNT(DISTINCT oi.order_id) AS orders FROM {OI} AS oi "
        f"JOIN {P} AS p ON p.id = oi.product_id "
        "WHERE oi.created_at >= TIMESTAMP('2025-01-01') "
        f"AND oi.created_at < TIMESTAMP('2026-01-01') AND {REVENUE_FILTER}"
    )
    manager = run_check(sql, user=MANAGER)
    ceo = run_check(sql, user=CEO)
    assert manager.period == ceo.period and manager.period.code == "outside"
    assert manager.definitions == ceo.definitions


# --- the definitions ------------------------------------------------------------------------------


def test_missing_status_filter_gives_feedback():
    check = run_check(SQL_2024_NO_STATUS)
    assert check.definitions.status == "mismatch"
    assert check.definitions.missing == ("orders",)
    assert check.feedback.startswith("The SQL does not apply the definition of orders:")
    assert "leaves out Cancelled and Returned" in check.feedback
    assert "oi.status NOT IN ('Cancelled', 'Returned')" in check.feedback


def test_both_checks_report_together():
    check = run_check(SQL_2025_NO_STATUS)
    assert check.period.code == "outside" and check.definitions.missing == ("orders",)
    assert "2025-01-01 to 2026-01-01" in check.feedback
    assert "definition of orders" in check.feedback


@pytest.mark.parametrize(
    ("condition", "keys", "ok"),
    [
        ("oi.status != 'Cancelled' AND oi.status != 'Returned'", ["orders"], True),
        ("NOT oi.status IN ('Cancelled', 'Returned')", ["revenue"], True),
        ("oi.status IN ('Complete', 'Shipped', 'Processing')", ["items_sold"], True),
        ("oi.status != 'Cancelled'", ["revenue"], False),
        ("oi.status != 'Cancelled'", ["purchase"], True),
        ("oi.status NOT IN ('Cancelled', 'Returned')", ["customer_of_brand"], False),
        ("oi.status = 'Complete' OR oi.sale_price > 10", ["orders"], False),
    ],
)
def test_status_filter_forms(condition, keys, ok):
    sql = (
        f"SELECT COUNT(DISTINCT oi.order_id) AS n FROM {OI} AS oi WHERE {condition} "
        "AND oi.created_at >= '2024-01-01' AND oi.created_at < '2025-01-01'"
    )
    check = run_check(sql, plan(definitions=keys))
    assert (check.definitions.status == "match") is ok


def test_status_filter_in_case_when_gets_the_where_of_its_select():
    sql = (
        "SELECT SUM(IF(oi.status != 'Returned', oi.sale_price, 0)) AS revenue, "
        "COUNT(DISTINCT oi.user_id) AS customers "
        f"FROM {OI} AS oi WHERE oi.status != 'Cancelled' "
        "AND oi.created_at >= '2024-01-01' AND oi.created_at < '2025-01-01'"
    )
    check = run_check(sql, plan(definitions=["revenue", "purchase"]))
    assert check.definitions.status == "match"


def test_metric_column_without_any_status_filter():
    """The model leaves `orders` out of definitions_used, but the column is an orders count."""
    check = run_check(SQL_2024_NO_STATUS, plan(definitions=()))
    assert check.definitions.missing == ("orders",)
    other = (
        f"SELECT COUNT(DISTINCT oi.order_id) AS cancelled_orders FROM {OI} AS oi "
        "WHERE oi.status = 'Cancelled' "
        "AND oi.created_at >= '2024-01-01' AND oi.created_at < '2025-01-01'"
    )
    assert run_check(other, plan(definitions=())).feedback == ""


def test_status_reader_returns_none_for_bad_sql():
    assert read_status_filters("SELECT FROM WHERE") is None
    assert read_date_filters("SELECT FROM WHERE", load_test_schema()) is None


# --- the flow with a fake model -------------------------------------------------------------------


def orders_frame(n: int):
    return frame_result(pd.DataFrame({"orders": [n]}))


def test_wrong_year_then_corrected(make_session, config):
    llm = FakeLLM(
        [
            route_reply(),
            sql_reply(SQL_2025, period=Y2024, definitions=["orders", "last_year"]),
            sql_reply(SQL_2024, period=Y2024, definitions=["orders"]),
            "20,500 orders were placed in 2024.",
        ]
    )
    bq = FakeBQ([orders_frame(20500)])
    session, _, _ = make_session("ceo", llm=llm, bq=bq)
    result = session.ask(QUESTION)
    assert result.error_type is None and len(bq.queries) == 1
    assert "TIMESTAMP('2024-01-01')" in bq.queries[0]
    retry = llm.calls[2]["prompt"]
    assert "Error type: sql_mismatch" in retry
    assert (
        "The period of the question is 1 Jan – 31 Dec 2024 (2024-01-01..2024-12-31), but the "
        "SQL filters order_items.created_at from 2025-01-01 to 2026-01-01." in retry
    )
    assert result.steps[0]["attempts"] == 2
    assert result.steps[0]["period"] == Y2024
    assert "Period: 1 Jan – 31 Dec 2024" in result.answer
    assert "Orders = orders with at least one item" in result.answer
    spans = [r for r in read_spans(config) if r["name"] == "node.check_sql"]
    assert [s["attributes"]["period_check"] for s in spans] == ["mismatch", "match"]
    assert spans[0]["attributes"]["period_check_code"] == "outside"


def test_missing_status_filter_then_corrected(make_session):
    llm = FakeLLM(
        [
            route_reply(),
            sql_reply(SQL_2024_NO_STATUS, period=Y2024),
            sql_reply(SQL_2024, period=Y2024),
            "20,500 orders.",
        ]
    )
    bq = FakeBQ([orders_frame(20500)])
    session, _, _ = make_session("ceo", llm=llm, bq=bq)
    result = session.ask(QUESTION)
    assert len(bq.queries) == 1 and "'Cancelled', 'Returned'" in bq.queries[0]
    assert "The SQL does not apply the definition of orders" in llm.calls[2]["prompt"]
    assert "Orders = orders with at least one item" in result.answer


def test_mismatch_after_the_last_attempt_fails_the_step(make_session):
    llm = FakeLLM([route_reply()] + [sql_reply(SQL_2025, period=Y2024)] * 3)
    session, _, bq = make_session("ceo", llm=llm)
    result = session.ask(QUESTION)
    assert bq.queries == []  # the wrong SQL never ran
    assert result.error_type == "sql_mismatch"
    assert result.answer.startswith("I could not get a valid result after 3 attempts.")
    assert "did not use the period or the metric definitions" in result.answer
    assert "Period:" not in result.answer


def test_manager_wrong_year_then_corrected_runs_scoped(make_session):
    sql = (
        f"SELECT COUNT(DISTINCT oi.order_id) AS orders FROM {OI} AS oi "
        f"JOIN {P} AS p ON p.id = oi.product_id "
        "WHERE oi.created_at >= TIMESTAMP('{0}') AND oi.created_at < TIMESTAMP('{1}') "
        f"AND {REVENUE_FILTER}"
    )
    llm = FakeLLM(
        [
            route_reply(),
            sql_reply(sql.format("2025-01-01", "2026-01-01"), period=Y2024),
            sql_reply(sql.format("2024-01-01", "2025-01-01"), period=Y2024),
            "Your brands had 900 orders in 2024.",
        ]
    )
    bq = FakeBQ([orders_frame(900)])
    session, _, _ = make_session("maya", llm=llm, bq=bq)
    result = session.ask("How many orders did my brands have in 2024?")
    assert len(bq.queries) == 1 and "scope_brands" in bq.queries[0]
    assert "TIMESTAMP('2024-01-01')" in bq.queries[0]
    assert (
        "Period: 1 Jan – 31 Dec 2024 · Brands: Calvin Klein, Carhartt" in result.answer
    )


def test_footer_takes_the_period_of_the_sql_when_none_is_stated(make_session):
    llm = FakeLLM([route_reply(), sql_reply(SQL_2024, period=None), "20,500 orders."])
    session, _, _ = make_session("ceo", llm=llm, bq=FakeBQ([orders_frame(20500)]))
    result = session.ask("How many orders were there?")
    assert "Period: 1 Jan – 31 Dec 2024" in result.answer
    assert "all data up to" not in result.answer


def test_footer_leaves_out_a_key_that_is_not_in_the_glossary(make_session):
    llm = FakeLLM(
        [
            route_reply(),
            sql_reply(SQL_2024, period=Y2024, definitions=["orders", "last_year"]),
            "20,500 orders.",
        ]
    )
    session, _, _ = make_session("ceo", llm=llm, bq=FakeBQ([orders_frame(20500)]))
    result = session.ask(QUESTION)
    assert result.steps[0]["definitions"] == ["orders"]
    assert result.steps[0]["definition_check"] == "match"


# --- cases from the code review: no false mismatch ------------------------------------------------


def test_unreadable_status_condition_is_unverified():
    sql = (
        f"SELECT COUNT(DISTINCT oi.order_id) AS orders FROM {OI} AS oi "
        "WHERE LOWER(oi.status) NOT IN ('cancelled', 'returned') "
        "AND oi.created_at >= '2024-01-01' AND oi.created_at < '2025-01-01'"
    )
    check = run_check(sql)
    assert check.definitions.status == "unverified" and check.feedback == ""


def test_one_revenue_filter_with_revenue_and_purchase_keys():
    """Code cannot tell which figure uses which filter, so the stricter filter passes when a
    cited key explains it."""
    sql = (
        "SELECT SUM(oi.sale_price) AS revenue, COUNT(DISTINCT oi.user_id) AS customers "
        f"FROM {OI} AS oi WHERE {REVENUE_FILTER} "
        "AND oi.created_at >= '2024-01-01' AND oi.created_at < '2025-01-01'"
    )
    check = run_check(sql, plan(definitions=["revenue", "purchase"]))
    assert check.definitions.status == "match"
    alone = run_check(sql, plan(definitions=["purchase"]))
    assert alone.definitions.missing == ("purchase",)


def test_metric_name_on_the_orders_table_is_not_a_glossary_figure():
    sql = (
        f"SELECT o.status, COUNT(*) AS orders FROM {T('orders')} AS o "
        "WHERE o.created_at >= '2024-01-01' AND o.created_at < '2025-01-01' GROUP BY o.status"
    )
    assert run_check(sql, plan(definitions=())).feedback == ""


def test_open_created_at_with_a_closed_delivery_period():
    sql = (
        f"SELECT COUNT(*) AS items FROM {OI} AS oi "
        "WHERE oi.delivered_at >= '2025-10-01' AND oi.delivered_at < '2025-11-01' "
        "AND oi.created_at >= '2025-01-01'"
    )
    check = run_check(
        sql,
        plan(period="2025-10-01..2025-10-31", definitions=()),
        question="Items delivered in October 2025?",
    )
    assert check.feedback == ""


def test_an_error_in_the_check_lets_the_query_run(make_session):
    """A stated period at the end of the calendar makes the date math fail: the check passes."""
    llm = FakeLLM(
        [route_reply(), sql_reply(SQL_2024, period="9999-12-30..9999-12-31"), "OK."]
    )
    session, _, bq = make_session("ceo", llm=llm, bq=FakeBQ([orders_frame(1)]))
    result = session.ask("How many orders?")
    assert len(bq.queries) == 1 and result.error_type is None
    assert result.steps[0]["period_check"] == "unverified"
