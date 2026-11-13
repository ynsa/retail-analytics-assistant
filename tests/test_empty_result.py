"""Empty-result checks (spec 30, B2.7 and B4 cases 70-76), with a fake BigQuery."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from opsfleet_agent.guard.empty_result import (
    MAX_PROBES,
    DiagnosisKind,
    EmptyResultChecker,
)
from opsfleet_agent.guard.sql_guard import SqlGuard
from tests.conftest import CEO, MANAGER, FakeBQ, T, frame_result

TODAY = date(2026, 11, 11)


def one_row(**values: int) -> object:
    return frame_result(pd.DataFrame({k: [v] for k, v in values.items()}))


@pytest.fixture
def make(test_schema, test_values):
    def _make(user=CEO, results=()):
        guard = SqlGuard(user, test_schema, test_values)
        bq = FakeBQ(list(results))
        return (
            guard,
            bq,
            EmptyResultChecker(guard, bq, test_values, today=lambda: TODAY),
        )

    return _make


def diagnose(make, sql, budget, user=CEO, results=()):
    guard, bq, checker = make(user, results)
    result = guard.check(sql)
    assert result.passed, result.model_feedback or result.user_message
    return checker.diagnose(result, budget), bq


def test_case_70_wrong_status_value_is_an_error_with_the_existing_values(make, budget):
    sql = f"SELECT COUNT(*) AS n FROM {T('orders')} WHERE status = 'completed'"
    diagnosis, bq = diagnose(make, sql, budget)
    assert (
        diagnosis.kind is DiagnosisKind.ERROR and diagnosis.finding == "value_missing"
    )
    assert (
        "Complete" in diagnosis.model_feedback
        and "completed" in diagnosis.model_feedback
    )
    assert diagnosis.queries_run == 0 and bq.queries == []


def test_case_71_period_before_the_data_is_a_true_zero_with_the_range(make, budget):
    sql = (
        f"SELECT SUM(sale_price) AS revenue FROM {T('order_items')} "
        "WHERE EXTRACT(YEAR FROM created_at) = 2015"
    )
    diagnosis, bq = diagnose(make, sql, budget)
    assert (
        diagnosis.kind is DiagnosisKind.TRUE_ZERO and diagnosis.finding == "before_data"
    )
    assert "2019-01-06" in diagnosis.user_note and "2026-11-11" in diagnosis.user_note
    assert diagnosis.queries_run == 0 and bq.queries == []


def test_period_after_today_is_a_true_zero(make, budget):
    sql = f"SELECT COUNT(*) AS n FROM {T('orders')} WHERE created_at >= '2027-01-01'"
    diagnosis, _ = diagnose(make, sql, budget)
    assert (
        diagnosis.kind is DiagnosisKind.TRUE_ZERO and diagnosis.finding == "after_today"
    )


def test_case_72_manager_brands_without_items_in_the_period(make, budget):
    sql = (
        f"SELECT SUM(sale_price) AS revenue FROM {T('order_items')} "
        "WHERE created_at >= TIMESTAMP('2024-01-01') AND created_at < TIMESTAMP('2024-02-01')"
    )
    diagnosis, bq = diagnose(make, sql, budget, MANAGER, [one_row(n=0)])
    assert (
        diagnosis.kind is DiagnosisKind.TRUE_ZERO
        and diagnosis.finding == "brand_no_sales"
    )
    assert diagnosis.queries_run == 1 and len(bq.queries) == 1
    assert "Calvin Klein and Levi's" in diagnosis.user_note
    assert "TIMESTAMP('2024-01-01')" in bq.queries[0]


def test_case_73_wrong_join_key_is_an_error(make, budget):
    sql = (
        f"SELECT COUNT(*) AS n FROM {T('orders')} o JOIN {T('users')} u ON u.id = o.order_id "
        "WHERE o.status = 'Complete'"
    )
    diagnosis, _bq = diagnose(
        make, sql, budget, CEO, [one_row(no_filters=0, table_0=5, table_1=10)]
    )
    assert diagnosis.kind is DiagnosisKind.ERROR and diagnosis.finding == "join_key"
    assert "check the join key" in diagnosis.model_feedback
    assert diagnosis.queries_run == 1


def test_filter_that_empties_the_result_is_named(make, budget):
    sql = (
        f"SELECT COUNT(*) AS n FROM {T('order_items')} "
        "WHERE status = 'Complete' AND sale_price > 5000"
    )
    diagnosis, bq = diagnose(
        make, sql, budget, CEO, [one_row(no_filters=100, drop_0=0, drop_1=40)]
    )
    assert (
        diagnosis.kind is DiagnosisKind.TRUE_ZERO
        and diagnosis.finding == "filter_empties"
    )
    assert "sale_price > 5000" in diagnosis.user_note
    assert "drop_0" in bq.queries[0] and "drop_1" in bq.queries[0]


def test_wrong_date_format_is_an_error(make, budget):
    sql = (
        f"SELECT COUNT(*) AS n FROM {T('order_items')} "
        "WHERE status = 'Complete' AND created_at >= '01/03/2024'"
    )
    diagnosis, _ = diagnose(
        make, sql, budget, CEO, [one_row(no_filters=100, drop_0=0, drop_1=40)]
    )
    assert diagnosis.kind is DiagnosisKind.ERROR and diagnosis.finding == "wrong_filter"


def test_value_of_an_uncached_column_is_checked_with_one_probe(make, budget):
    sql = f"SELECT COUNT(*) AS n FROM {T('users')} WHERE city = 'Sao Paolo'"
    diagnosis, bq = diagnose(make, sql, budget, CEO, [one_row(v0=0)])
    assert (
        diagnosis.kind is DiagnosisKind.ERROR and diagnosis.finding == "value_missing"
    )
    assert "COUNTIF" in bq.queries[0] and diagnosis.queries_run == 1


def test_case_74_nine_conditions_skip_check_4(make, budget):
    conds = " AND ".join(f"sale_price > {i}" for i in range(9))
    sql = f"SELECT COUNT(*) AS n FROM {T('order_items')} WHERE {conds}"
    diagnosis, bq = diagnose(make, sql, budget, MANAGER, [one_row(n=10)])
    assert diagnosis.kind is DiagnosisKind.TRUE_ZERO
    assert len(bq.queries) == 1  # only check 3; check 4 is skipped
    assert diagnosis.queries_run <= MAX_PROBES


def test_case_75_no_queries_left_gives_unknown(make, budget):
    for _ in range(budget.queries_left()):
        budget.charge_query()
    sql = f"SELECT SUM(sale_price) AS r FROM {T('order_items')} WHERE sale_price > 5000"
    diagnosis, bq = diagnose(make, sql, budget, MANAGER)
    assert diagnosis.kind is DiagnosisKind.UNKNOWN and diagnosis.queries_run == 0
    assert bq.queries == [] and "did not run" in diagnosis.user_note


def test_checks_stop_when_the_budget_runs_out_between_probes(make, budget):
    for _ in range(budget.queries_left() - 1):
        budget.charge_query()
    sql = (
        f"SELECT COUNT(*) AS n FROM {T('order_items')} "
        "WHERE status = 'Complete' AND sale_price > 5000"
    )
    diagnosis, _bq = diagnose(make, sql, budget, MANAGER, [one_row(n=10)])
    assert diagnosis.kind is DiagnosisKind.UNKNOWN and diagnosis.queries_run == 1


def test_case_76_each_manager_probe_goes_through_the_guard_with_the_scope(make, budget):
    sql = (
        f"SELECT COUNT(*) AS n FROM {T('order_items')} oi JOIN {T('products')} p "
        "ON p.id = oi.product_id WHERE oi.status = 'Complete' AND oi.sale_price > 5000"
    )
    diagnosis, bq = diagnose(
        make,
        sql,
        budget,
        MANAGER,
        [
            one_row(n=10),
            one_row(no_filters=50, drop_0=0, drop_1=3, table_0=4, table_1=9),
        ],
    )
    assert len(bq.queries) == 2 and diagnosis.queries_run == 2
    for probe, params in zip(bq.queries, bq.params, strict=True):
        assert "IN UNNEST(@scope_brands)" in probe
        assert params and params[0].values == ["Calvin Klein", "Levi's"]
    assert set(bq.purposes) == {"empty_check"}
    assert (
        diagnosis.finding == "filter_empties"
        and "sale_price > 5000" in diagnosis.user_note
    )


def test_never_more_than_five_probes(make, budget, monkeypatch):
    guard, bq, checker = make(MANAGER, [one_row(n=1)] * 10)
    result = guard.check(f"SELECT COUNT(*) AS n FROM {T('order_items')}")
    from opsfleet_agent.guard import empty_result

    run = empty_result._Run(budget)
    for _ in range(MAX_PROBES):
        checker._probe(f"SELECT COUNT(*) AS n FROM {T('orders')}", run)
    with pytest.raises(empty_result._OutOfBudget):
        checker._probe(f"SELECT COUNT(*) AS n FROM {T('orders')}", run)
    assert len(bq.queries) == MAX_PROBES and result.passed
