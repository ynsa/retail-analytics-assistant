"""Chart checks, SVG values and Mermaid text (spec 32, B4 cases 8, 9, 10 and 13)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from opsfleet_agent.agent.formatting import store_result
from opsfleet_agent.config import ReportSettings
from opsfleet_agent.guard.masker import MaskedResult
from opsfleet_agent.reports.charts import (
    check_chart,
    check_charts,
    draw_svg,
    to_mermaid,
)
from opsfleet_agent.reports.models import ChartSpec

FIXTURES = Path(__file__).parent / "fixtures"
SETTINGS = ReportSettings()


def result(ref: str, columns: list[str], rows: list[list]):
    masked = MaskedResult(
        columns=columns, rows=rows, row_count=len(rows), truncated=False
    )
    return store_result(masked, ref, "test")


MONTHS = result(
    "r1",
    ["month", "revenue", "orders"],
    [["2026-01", 1200.5, 10], ["2026-02", 1300.25, 12], ["2026-03", 900.0, 8]],
)
BRANDS = result(
    "r2", ["brand", "revenue"], [[f"Brand {i:02d}", float(100 + i)] for i in range(25)]
)
SHARES = result(
    "r3",
    ["category", "revenue", "returns_pct"],
    [["A", 10.0, 1.0], ["B", 20.0, -2.0], ["C", 30.0, 3.0]],
)
RESULTS = {r["ref"]: r for r in (MONTHS, BRANDS, SHARES)}


def chart(**fields):
    return ChartSpec(
        **{
            "ref": "r1",
            "type": "line",
            "x": "month",
            "y": ["revenue"],
            "title": "T",
            **fields,
        }
    )


# --- case 8: each row of the check table ---------------------------------------------------------
def test_missing_ref_drops_the_chart():
    data, notes = check_chart(chart(ref="r9"), RESULTS, SETTINGS)
    assert data is None and "result is missing" in notes[0]


@pytest.mark.parametrize("fields", [{"x": "day"}, {"y": ["profit"]}, {"y": []}])
def test_missing_column_drops_the_chart(fields):
    data, notes = check_chart(chart(**fields), RESULTS, SETTINGS)
    assert data is None and "column is missing" in notes[0]


def test_text_y_column_drops_the_chart():
    data, notes = check_chart(chart(y=["month"]), RESULTS, SETTINGS)
    assert data is None and "not a number" in notes[0]


def test_more_than_three_y_columns_keeps_the_first_three():
    many = result(
        "r4",
        ["month", "a_revenue", "b_revenue", "c_revenue", "d_revenue"],
        [["2026-01", 1.0, 2.0, 3.0, 4.0]],
    )
    data, notes = check_chart(
        chart(ref="r4", y=["a_revenue", "b_revenue", "c_revenue", "d_revenue"]),
        {"r4": many},
        SETTINGS,
    )
    assert (
        data["y"] == ["a_revenue", "b_revenue", "c_revenue"] and "first 3" in notes[0]
    )


def test_line_with_text_x_becomes_bar():
    data, _ = check_chart(
        chart(ref="r2", x="brand", y=["revenue"], type="line"), RESULTS, SETTINGS
    )
    assert data["type"] == "bar"


def test_line_with_more_than_60_points_becomes_bar():
    rows = [
        [f"2020-{m:02d}" if m <= 12 else f"20{20 + m // 12}-{m % 12 + 1:02d}", float(m)]
        for m in range(1, 62)
    ]
    long = result("r5", ["month", "revenue"], rows)
    data, _ = check_chart(chart(ref="r5"), {"r5": long}, SETTINGS)
    assert data["type"] == "bar" and len(data["labels"]) == SETTINGS.max_bar_categories


def test_line_with_dates_stays_line_and_is_sorted():
    shuffled = result(
        "r6",
        ["month", "revenue"],
        [["2026-03", 3.0], ["2026-01", 1.0], ["2026-02", 2.0]],
    )
    data, _ = check_chart(chart(ref="r6"), {"r6": shuffled}, SETTINGS)
    assert data["type"] == "line" and data["labels"] == [
        "2026-01",
        "2026-02",
        "2026-03",
    ]


def test_bar_with_more_than_20_categories_keeps_the_top_20():
    data, notes = check_chart(chart(ref="r2", x="brand", type="bar"), RESULTS, SETTINGS)
    assert len(data["labels"]) == 20 and data["labels"][0] == "Brand 24"
    assert "top 20 of 25" in notes[0]


@pytest.mark.parametrize(
    "fields",
    [
        {"ref": "r2", "x": "brand", "y": ["revenue"]},  # more than 6 slices
        {"ref": "r3", "x": "category", "y": ["returns_pct"]},  # a negative value
        {
            "ref": "r3",
            "x": "category",
            "y": ["revenue", "returns_pct"],
        },  # two y columns
    ],
)
def test_pie_that_does_not_fit_becomes_bar(fields):
    data, _ = check_chart(chart(type="pie", **fields), RESULTS, SETTINGS)
    assert data["type"] == "bar"


def test_good_pie_stays_pie():
    data, _ = check_chart(chart(ref="r3", x="category", type="pie"), RESULTS, SETTINGS)
    assert data["type"] == "pie"


def test_other_chart_type_becomes_bar():
    data, _ = check_chart(chart(type="scatter"), RESULTS, SETTINGS)
    assert data["type"] == "bar" and data["requested_type"] == "scatter"


def test_more_than_three_charts_keeps_the_first_three():
    charts, notes = check_charts(
        [chart(title=f"C{i}") for i in range(5)], RESULTS, SETTINGS
    )
    assert [c["title"] for c in charts] == [
        "C0",
        "C1",
        "C2",
    ] and "first 3 charts" in notes[-1]


def test_chart_data_is_a_copy_of_the_result_values():
    data, _ = check_chart(chart(y=["revenue", "orders"]), RESULTS, SETTINGS)
    assert data["series"] == {
        "revenue": [1200.5, 1300.25, 900.0],
        "orders": [10.0, 12.0, 8.0],
    }


# --- case 9: the values in the SVG ----------------------------------------------------------------
@pytest.mark.parametrize("kind", ["line", "bar"])
def test_svg_values_equal_the_result_values(kind):
    data, _ = check_chart(chart(type=kind, y=["revenue", "orders"]), RESULTS, SETTINGS)
    svg = draw_svg(data)
    prefix = "pt" if kind == "line" else "bar"
    found = {
        (int(s), int(i)): float(v)
        for s, i, v in re.findall(rf'id="{prefix}_s(\d)_i(\d+)_v([-\d.e+]+)"', svg)
    }
    expected = {
        (s, i): float(row[col])
        for s, col in enumerate((1, 2))
        for i, row in enumerate(MONTHS["rows"])
    }
    assert found == expected


def test_pie_svg_values_equal_the_result_values():
    data, _ = check_chart(chart(ref="r3", x="category", type="pie"), RESULTS, SETTINGS)
    values = [
        float(v) for v in re.findall(r'id="pie_s0_i\d+_v([-\d.e+]+)"', draw_svg(data))
    ]
    assert values == [10.0, 20.0, 30.0]


# --- case 10: the same data gives the same bytes -------------------------------------------------
def test_same_chart_twice_gives_the_same_svg():
    data, _ = check_chart(chart(), RESULTS, SETTINGS)
    first, second = draw_svg(data), draw_svg(data)
    assert first == second and first.startswith("<svg")
    assert "http" not in first and "<script" not in first and "<metadata" not in first


# --- case 13: Mermaid with a double quote in a label ---------------------------------------------
def test_mermaid_label_with_quote_matches_the_fixture():
    quoted = result(
        "r7",
        ["product", "revenue"],
        [['Jeans "Slim" 32/34 extra long fit edition', 120.5], ["Coat", 80.0]],
    )
    data, _ = check_chart(
        ChartSpec(
            ref="r7",
            type="bar",
            x="product",
            y=["revenue"],
            title='Top "hero" products',
        ),
        {"r7": quoted},
        SETTINGS,
    )
    assert to_mermaid(data) + "\n" == (FIXTURES / "mermaid_quote.md").read_text(
        encoding="utf-8"
    )


def test_mermaid_pie_block():
    data, _ = check_chart(
        chart(ref="r3", x="category", type="pie", title="Share"), RESULTS, SETTINGS
    )
    assert (
        to_mermaid(data)
        == '```mermaid\npie title "Share"\n    "A" : 10\n    "B" : 20\n    "C" : 30\n```'
    )
