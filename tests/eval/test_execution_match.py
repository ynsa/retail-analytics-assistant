"""Execution match (spec 21 B2.6, spec 23 B4): mapping, order, top_k ties, tolerance. No network."""

from __future__ import annotations

from typing import Any

from opsfleet_agent.evals.cases import Expected
from opsfleet_agent.evals.records import QueryRecord
from opsfleet_agent.evals.scorers.execution_match import execution_match


def exp(mode: str, k: int | None = None, **cols: Any) -> Expected:
    return Expected.model_validate({"mode": mode, "k": k, "columns": cols})


def q(
    columns: list[str], rows: list[list[Any]], error: str | None = None
) -> QueryRecord:
    return QueryRecord(sql="SELECT 1", columns=columns, rows=rows, error_type=error)


BRANDS = exp("rows", brand={"role": "key"}, revenue={"role": "value", "kind": "money"})
REF_COLS = ["brand", "revenue"]
REF_ROWS = [["Calvin Klein", 37217.50], ["Carhartt", 25707.90]]


def test_rows_match_with_other_names_and_extra_columns():
    agent = q(
        ["b_name", "items", "total_rev"],
        [["Carhartt", 385, 25707.9], ["calvin klein ", 556, 37217.5]],
    )
    result = execution_match(BRANDS, REF_COLS, REF_ROWS, [agent])
    assert result.passed, result.details
    assert result.details["mapping"] == {"brand": "b_name", "revenue": "total_rev"}


def test_missing_value_column_fails():
    agent = q(["brand", "items"], [["Carhartt", 385], ["Calvin Klein", 556]])
    result = execution_match(BRANDS, REF_COLS, REF_ROWS, [agent])
    assert not result.passed and result.reason == "wrong_value"


def test_missing_key_column_fails():
    agent = q(["revenue"], [[37217.5], [25707.9]])
    result = execution_match(BRANDS, REF_COLS, REF_ROWS, [agent])
    assert not result.passed and result.reason == "missing_column"


def test_extra_row_fails_rows_mode():
    agent = q(REF_COLS, [*REF_ROWS, ["Levi's", 15024.78]])
    result = execution_match(BRANDS, REF_COLS, REF_ROWS, [agent])
    assert not result.passed and result.reason == "wrong_rows"


def test_contains_mode_allows_more_rows():
    contains = exp(
        "contains", brand={"role": "key"}, revenue={"role": "value", "kind": "money"}
    )
    agent = q(REF_COLS, [*REF_ROWS, ["Levi's", 15024.78]])
    assert execution_match(contains, REF_COLS, REF_ROWS, [agent]).passed


def test_tolerance_half_percent():
    near = q(REF_COLS, [["Calvin Klein", 37217.50 * 1.004], ["Carhartt", 25707.90]])
    far = q(REF_COLS, [["Calvin Klein", 37217.50 * 1.01], ["Carhartt", 25707.90]])
    assert execution_match(BRANDS, REF_COLS, REF_ROWS, [near]).passed
    assert execution_match(BRANDS, REF_COLS, REF_ROWS, [far]).reason == "wrong_value"


def test_counts_match_exactly():
    e = exp("value", orders={"role": "value", "kind": "count"})
    assert execution_match(e, ["orders"], [[90]], [q(["n"], [[90]])]).passed
    assert not execution_match(e, ["orders"], [[90]], [q(["n"], [[91]])]).passed


def test_ratio_also_matches_as_percent():
    e = exp("value", rate={"role": "value", "kind": "ratio"})
    assert execution_match(e, ["rate"], [[0.1234]], [q(["pct"], [[12.34]])]).passed
    assert execution_match(e, ["rate"], [[0.1234]], [q(["r"], [[0.1235]])]).passed
    assert not execution_match(e, ["rate"], [[0.1234]], [q(["r"], [[0.15]])]).passed


def test_value_mode_finds_the_figure_in_the_column_with_the_same_meaning():
    e = exp("value", revenue={"role": "value", "kind": "money"})
    agent = q(["orders", "revenue_usd"], [[6927, 580425.72]])
    result = execution_match(e, ["revenue"], [[580425.7207]], [agent])
    assert result.passed and result.details["rule"] == "column_name"
    sales = q(["total_sales", "orders"], [[580425.72, 6927]])  # a synonym
    assert execution_match(e, ["revenue"], [[580425.7207]], [sales]).passed


def test_value_mode_wrong_column_with_the_same_value_fails():
    """Spec 21 B7 review point: orders = 800 must not pass for a question about customers."""
    e = exp("value", customers={"role": "value", "kind": "count"})
    agent = q(["customers", "orders"], [[750, 800]])
    result = execution_match(e, ["customers"], [[800]], [agent])
    assert not result.passed and result.reason == "wrong_value"
    assert result.details["problem"] == "column_meaning"
    assert result.details["columns"] == ["orders"]
    unnamed = q(["f0_", "f1_"], [[800, 3]])  # two numbers, no meaning in the names
    assert not execution_match(e, ["customers"], [[800]], [unnamed]).passed


def test_value_mode_only_number_column_needs_no_name():
    e = exp("value", customers={"role": "value", "kind": "count"})
    one = q(["n"], [[800]])
    result = execution_match(e, ["customers"], [[800]], [one])
    assert result.passed and result.details["rule"] == "only_number_column"
    with_year = q(["year", "total"], [[2025, 800]])  # a period column is not the answer
    assert execution_match(e, ["customers"], [[800]], [with_year]).passed
    with_label = q(
        ["metric", "total"], [["buyers", 800]]
    )  # a text column is not a number
    assert execution_match(e, ["customers"], [[800]], [with_label]).passed
    rate = exp("value", repeat_rate={"role": "value", "kind": "ratio"})
    share = q(["customers", "repeat_customer_share"], [[1200, 0.0812]])
    assert execution_match(rate, ["repeat_rate"], [[0.0812]], [share]).passed


def test_rows_ordered_rule():
    months = exp(
        "rows_ordered",
        month={"role": "key", "kind": "month"},
        revenue={"role": "value", "kind": "money"},
    )
    ref = [["2025-01-01", 10.0], ["2025-02-01", 20.0]]
    good = q(["m", "rev"], [["2025-01", 10.0], ["2025-02", 20.0]])
    wrong_order = q(["m", "rev"], [["2025-02", 20.0], ["2025-01", 10.0]])
    assert execution_match(months, ["month", "revenue"], ref, [good]).passed
    result = execution_match(months, ["month", "revenue"], ref, [wrong_order])
    assert result.reason == "wrong_order"


def test_month_as_number_or_name_in_one_year():
    months = exp(
        "rows",
        month={"role": "key", "kind": "month"},
        revenue={"role": "value", "kind": "money"},
    )
    ref = [["2025-01-01", 10.0], ["2025-02-01", 20.0]]
    as_int = q(["month_num", "rev"], [[1, 10.0], [2, 20.0]])
    as_name = q(["month_name", "rev"], [["January", 10.0], ["February", 20.0]])
    assert execution_match(months, ["month", "revenue"], ref, [as_int]).passed
    assert execution_match(months, ["month", "revenue"], ref, [as_name]).passed


def test_quarter_and_year_keys():
    quarters = exp(
        "rows",
        quarter={"role": "key", "kind": "quarter"},
        r={"role": "value", "kind": "money"},
    )
    ref = [["2025-01-01", 1.0], ["2025-04-01", 2.0]]
    agent = q(["q", "r"], [["Q1 2025", 1.0], ["2025-Q2", 2.0]])
    assert execution_match(quarters, ["quarter", "r"], ref, [agent]).passed
    years = exp(
        "rows",
        year={"role": "key", "kind": "year"},
        r={"role": "value", "kind": "money"},
    )
    agent = q(["y", "r"], [[2025.0, 1.0], ["2026-01-01", 2.0]])
    assert execution_match(
        years, ["year", "r"], [[2025, 1.0], [2026, 2.0]], [agent]
    ).passed


def test_gender_letters_match_words():
    e = exp("rows", gender={"role": "key"}, aov={"role": "value", "kind": "money"})
    agent = q(["gender", "aov"], [["Female", 81.08], ["Male", 91.51]])
    assert execution_match(
        e, ["gender", "aov"], [["F", 81.0832], ["M", 91.5097]], [agent]
    ).passed


def test_top_k_with_tie_at_the_border():
    e = exp(
        "top_k", k=2, user_id={"role": "key", "kind": "id"}, spend={"role": "value"}
    )
    ref = [[1, 300.0], [2, 260.0], [3, 260.0], [4, 100.0]]
    tie_pick = q(["user_id", "spend"], [[1, 300.0], [3, 260.0]])
    outside = q(["user_id", "spend"], [[1, 300.0], [4, 100.0]])
    too_short = q(["user_id", "spend"], [[1, 300.0]])
    assert execution_match(e, ["user_id", "spend"], ref, [tie_pick]).passed
    assert (
        execution_match(e, ["user_id", "spend"], ref, [outside]).reason == "wrong_rows"
    )
    assert (
        execution_match(e, ["user_id", "spend"], ref, [too_short]).reason
        == "wrong_rows"
    )


def test_one_matching_result_is_enough():
    wrong = q(REF_COLS, [["Carhartt", 1.0]])
    right = q(REF_COLS, REF_ROWS)
    result = execution_match(BRANDS, REF_COLS, REF_ROWS, [wrong, right])
    assert result.passed and result.details["query"] == 1


def test_no_result_and_failed_queries():
    assert execution_match(BRANDS, REF_COLS, REF_ROWS, []).reason == "no_result"
    failed = q([], [], error="bq_syntax")
    assert execution_match(BRANDS, REF_COLS, REF_ROWS, [failed]).reason == "no_result"


def test_empty_results():
    e = exp("value", items={"role": "value", "kind": "count"})
    assert execution_match(e, ["items"], [[0]], [q(["items"], [])]).passed
    assert not execution_match(BRANDS, REF_COLS, REF_ROWS, [q(REF_COLS, [])]).passed


def test_custom_absolute_tolerance():
    e = Expected.model_validate(
        {
            "mode": "value",
            "abs_tol": 0.15,
            "columns": {"days": {"role": "value", "kind": "number"}},
        }
    )
    assert execution_match(e, ["days"], [[3.05]], [q(["d"], [[3.15]])]).passed
    assert not execution_match(e, ["days"], [[3.05]], [q(["d"], [[2.66]])]).passed


# --- A comparison as one wide row (spec 50 round 1, TM-05) ---------------------------------------
YEARS = exp(
    "rows",
    year={"role": "key", "kind": "year"},
    revenue={"role": "value", "kind": "money"},
)
YEAR_ROWS = [[2025, 11102.39], [2026, 19024.21]]


def test_wide_comparison_row_matches_one_row_for_each_year():
    agent = q(
        ["revenue", "revenue_prev", "revenue_change_pct"], [[19024.21, 11102.39, 71.35]]
    )
    result = execution_match(YEARS, ["year", "revenue"], YEAR_ROWS, [agent])
    assert result.passed, result.details
    assert result.details["rule"] == "wide_row"
    agent = q(["revenue_2025", "revenue_2026"], [[11102.39, 19024.21]])
    assert execution_match(YEARS, ["year", "revenue"], YEAR_ROWS, [agent]).passed


def test_wide_row_with_a_wrong_value_or_a_wrong_meaning_fails():
    wrong = q(["revenue", "revenue_prev"], [[19024.21, 11000.0]])
    result = execution_match(YEARS, ["year", "revenue"], YEAR_ROWS, [wrong])
    assert not result.passed and result.reason == "missing_column"
    other = q(
        ["revenue", "orders"], [[19024.21, 11102.39]]
    )  # the meaning is not revenue
    assert not execution_match(YEARS, ["year", "revenue"], YEAR_ROWS, [other]).passed
    same_column = q(["revenue"], [[19024.21]])  # one value cannot stand for two rows
    assert not execution_match(
        YEARS, ["year", "revenue"], YEAR_ROWS, [same_column]
    ).passed
