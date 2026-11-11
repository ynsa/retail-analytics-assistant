"""Number formatting, periods, calculation placeholders and the figure check (spec 31).

No model and no query: pure code.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from opsfleet_agent.agent.calc import (
    NOT_AVAILABLE,
    check_figures,
    numbers_in,
    resolve_placeholders,
    result_numbers,
)
from opsfleet_agent.agent.formatting import (
    ColumnKind,
    ResultData,
    column_kind,
    data_block,
    format_value,
    plain_value,
    store_result,
)
from opsfleet_agent.agent.periods import Calendar, format_range, parse_periods, span_of
from opsfleet_agent.guard.masker import MaskedResult

REMOVED = "[figure removed: not traceable]"


# --- formatting -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "values", "kind"),
    [
        ("revenue", [385083.240183115], ColumnKind.MONEY),
        ("average_order_value", [82.4], ColumnKind.MONEY),
        ("orders", [6290], ColumnKind.COUNT),
        ("items_sold", [355], ColumnKind.COUNT),
        ("n", [3, 4], ColumnKind.COUNT),
        ("return_rate", [0.124], ColumnKind.RATE),
        ("return_rate", [12.4], ColumnKind.PERCENT),
        ("revenue_change_pct", [-5.3], ColumnKind.PERCENT),
        ("user_id", [123], ColumnKind.ID),
        ("year", [2026], ColumnKind.YEAR),
        ("brand", ["Carhartt"], ColumnKind.TEXT),
        ("score", [1.5], ColumnKind.NUMBER),
        ("revenue", [None], ColumnKind.TEXT),
        (
            "generated_revenue",
            [13836.87],
            ColumnKind.MONEY,
        ),  # "rate" is not a word here
        ("avg_items_per_order", [1.2], ColumnKind.COUNT),
        ("num_buyers", [3], ColumnKind.COUNT),
        ("revenue_per_customer", [60.5], ColumnKind.MONEY),
    ],
)
def test_column_kind(name, values, kind):
    assert column_kind(name, values) is kind


@pytest.mark.parametrize(
    ("value", "kind", "text"),
    [
        (385083.240183115, ColumnKind.MONEY, "$385,083.24"),
        (-1234.5, ColumnKind.MONEY, "-$1,234.50"),
        (6290, ColumnKind.COUNT, "6,290"),
        (1.456, ColumnKind.COUNT, "1.46"),
        (0.12449, ColumnKind.RATE, "12.4%"),
        (12.76, ColumnKind.PERCENT, "12.8%"),
        (2026, ColumnKind.YEAR, "2026"),
        (12345, ColumnKind.ID, "12345"),
        (1234.5678, ColumnKind.NUMBER, "1,234.57"),
        ("Levi's", ColumnKind.TEXT, "Levi's"),
        (None, ColumnKind.MONEY, ""),
    ],
)
def test_format_value(value, kind, text):
    assert format_value(value, kind) == text


def test_signed_values():
    assert format_value(5.25, ColumnKind.PERCENT, signed=True) == "+5.2%"
    assert format_value(-3.0, ColumnKind.PERCENT, signed=True) == "-3.0%"
    assert format_value(10.0, ColumnKind.MONEY, signed=True) == "+$10.00"


def test_plain_value_converts_bigquery_types():
    assert plain_value(np.int64(5)) == 5 and isinstance(plain_value(np.int64(5)), int)
    assert plain_value(Decimal("1.50")) == 1.5
    assert plain_value(pd.Timestamp("2026-10-01")) == "2026-10-01"
    assert plain_value(pd.Timestamp("2026-10-01 10:30:00")) == "2026-10-01T10:30:00"
    assert plain_value(date(2026, 10, 1)) == "2026-10-01"
    assert plain_value(float("nan")) is None and plain_value(pd.NA) is None


def _result(columns, rows, ref="r1") -> ResultData:
    masked = MaskedResult(
        columns=columns,
        rows=[tuple(r) for r in rows],
        row_count=len(rows),
        truncated=False,
    )
    return store_result(masked, ref, "test")


def test_data_block_has_formatted_values_only():
    result = _result(
        ["month", "revenue", "orders"], [["2026-10", 385083.240183115, 6290]]
    )
    block = data_block(result)
    assert block.startswith('<data source="query" ref="r1" rows="1">')
    assert "| 2026-10 | $385,083.24 | 6,290 |" in block
    assert "385083.24" not in block and block.endswith("</data>")


def test_data_block_cuts_rows_with_a_note():
    result = _result(["n"], [[i] for i in range(30)])
    assert "(10 more rows cut for size)" in data_block(result, max_rows=20)


# --- periods --------------------------------------------------------------------------------------


def test_calendar_periods():
    cal = Calendar(date(2026, 11, 11))
    assert (cal.last_month.start, cal.last_month.end) == (
        date(2026, 10, 1),
        date(2026, 10, 31),
    )
    assert cal.month_before_last.start == date(2026, 9, 1)
    assert cal.last_month.end_exclusive == date(2026, 11, 1)
    assert (cal.last_quarter.start, cal.last_quarter.end) == (
        date(2026, 7, 1),
        date(2026, 9, 30),
    )
    assert cal.this_quarter.start == date(2026, 10, 1)
    assert cal.last_year.end == date(2025, 12, 31)
    january = Calendar(date(2026, 1, 15))
    assert january.last_month.start == date(2025, 12, 1)
    assert january.last_quarter.start == date(2025, 10, 1)


def test_format_range():
    assert format_range(date(2026, 10, 1), date(2026, 10, 31)) == "1–31 Oct 2026"
    assert format_range(date(2026, 9, 1), date(2026, 10, 31)) == "1 Sep – 31 Oct 2026"
    assert (
        format_range(date(2025, 12, 1), date(2026, 1, 31)) == "1 Dec 2025 – 31 Jan 2026"
    )
    assert format_range(date(2026, 1, 5), date(2026, 1, 5)) == "5 Jan 2026"


def test_parse_periods():
    periods = parse_periods("2026-10-01..2026-10-31 and 2026-09-01 to 2026-09-30")
    assert span_of(periods).text() == "1 Sep – 31 Oct 2026"
    assert parse_periods("since 2026-01-01")[0].start == date(2026, 1, 1)
    assert parse_periods("last month") == [] and parse_periods(None) == []
    assert parse_periods("2026-13-01..2026-14-01") == []


# --- calculation placeholders ---------------------------------------------------------------------


@pytest.fixture
def results() -> list[ResultData]:
    by_month = _result(
        ["month", "brand", "revenue", "orders"],
        [
            ["2026-10", "Carhartt", 1100.0, 11],
            ["2026-09", "Carhartt", 1000.0, 10],
            ["2026-10", "Calvin Klein", 500.0, 5],
        ],
    )
    total = _result(["revenue", "return_rate"], [[1600.0, 0.1]], ref="r2")
    return [by_month, total]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "{{calc:pct_change(r1.revenue@Carhartt&2026-10, r1.revenue@Carhartt&2026-09)}}",
            "+10.0%",
        ),
        (
            "{{calc:diff(r1.revenue@Carhartt&2026-10, r1.revenue@Carhartt&2026-09)}}",
            "+$100.00",
        ),
        ("{{calc:share(r1.revenue@Calvin Klein, r2.revenue)}}", "31.2%"),
        ("{{calc:ratio(r1.orders@Carhartt&2026-10, r1.orders@Calvin Klein)}}", "2.20"),
        ("{{calc:sum(r1.revenue)}}", "$2,600.00"),
        ("{{ calc: avg(r1.orders) }}", "8.67"),
        ("{{calc:sum(r1.revenue@Carhartt)}}", "$2,100.00"),
    ],
)
def test_placeholders_are_computed_by_code(results, text, expected):
    outcome = resolve_placeholders(f"Value: {text}.", results)
    assert outcome.text == f"Value: {expected}." and outcome.errors == 0
    assert len(outcome.computed) == 1


@pytest.mark.parametrize(
    "text",
    [
        "{{calc:pct_change(r9.revenue, r1.revenue@2026-09)}}",  # unknown result
        "{{calc:diff(r1.margin@2026-10, r1.revenue@2026-09)}}",  # unknown column
        "{{calc:diff(r1.revenue@2026-10, r1.revenue@2026-09)}}",  # two rows match 2026-10
        "{{calc:diff(r1.revenue@2026-10, 1000)}}",  # a number is not an input
        "{{calc:pct_change(r2.revenue, r2.missing)}}",
        "{{calc:power(r2.revenue, r2.revenue)}}",  # not an allowed operation
        "{{calc:diff(r1.brand@2026-10&Carhartt, r2.revenue)}}",  # not a number
    ],
)
def test_bad_placeholders_become_not_available(results, text):
    errors: list[str] = []
    outcome = resolve_placeholders(text, results, on_error=errors.append)
    assert outcome.text == NOT_AVAILABLE and outcome.errors == 1 and len(errors) == 1


def test_division_by_zero_is_not_available():
    zero = [_result(["a", "b"], [[1.0, 0.0]])]
    assert (
        resolve_placeholders("{{calc:ratio(r1.a, r1.b)}}", zero).text == NOT_AVAILABLE
    )


# --- figure check ---------------------------------------------------------------------------------


def test_figures_from_results_dates_and_question_pass(results):
    allowed = result_numbers(results) + numbers_in("top 25 products in 2026")
    text = (
        "Carhartt had $1,100.00 in Oct 2026 (1–31 Oct 2026, 2026-10-01) with 11 orders. "
        "Return rate 10.0%. Total $1.6K. The top 25 products. 3 brands. Q3 2026 and 1st place."
    )
    check = check_figures(text, allowed, REMOVED)
    assert check.text == text and check.unverified == 0


def test_rounding_forms_pass():
    allowed = [385083.240183115, 0.124, 12.76]
    text = "$385,083.24, $385,083, $385.1K, $0.39M, 12.8%, 12.76"
    assert check_figures(text, allowed, REMOVED).unverified == 0


def test_invented_figures_are_removed(results):
    text = "Revenue grew by 17.5% to $1,234.56. That is 4,321 orders."
    check = check_figures(text, result_numbers(results), REMOVED)
    assert check.unverified == 3 and check.checked == 3
    assert (
        check.text
        == f"Revenue grew by {REMOVED} to {REMOVED}. That is {REMOVED} orders."
    )


def test_negative_and_signed_figures(results):
    allowed = result_numbers(results) + [-10.0]
    check = check_figures("Change: -10.0%, -$1,000.00, +$1,100.00", allowed, REMOVED)
    assert check.unverified == 0
