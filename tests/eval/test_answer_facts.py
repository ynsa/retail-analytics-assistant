"""Text scorers (spec 21 B2.6, spec 23 B4): figure formats, top-k names, required words."""

from __future__ import annotations

import pytest

from opsfleet_agent.evals.cases import Expected, FactSpec, RequiredSpec
from opsfleet_agent.evals.scorers.answer_facts import answer_facts, required_facts
from opsfleet_agent.evals.scorers.report_structure import report_file, report_structure
from opsfleet_agent.evals.textnum import contains_figure, extract_figures

BRANDS = Expected.model_validate(
    {
        "mode": "rows",
        "columns": {
            "brand": {"role": "key"},
            "revenue": {"role": "value", "kind": "money"},
        },
    }
)
COLS = ["brand", "revenue"]
ROWS = [["Carhartt", 25707.8999], ["Calvin Klein", 37217.5001]]
FACT = [FactSpec(column="revenue", where={"brand": "Calvin Klein"})]


@pytest.mark.parametrize(
    "text",
    [
        "Calvin Klein had 37,218 USD of revenue.",
        "Calvin Klein: $37.2K.",
        "Calvin Klein made 37.2 thousand dollars.",
        "Revenue was $37,217.50 for Calvin Klein.",
    ],
)
def test_money_formats(text: str):
    assert answer_facts(FACT, BRANDS, COLS, ROWS, text).passed


def test_missing_or_wrong_figure_fails():
    result = answer_facts(FACT, BRANDS, COLS, ROWS, "Calvin Klein had 39,000 USD.")
    assert not result.passed and result.reason == "fact_missing"


def test_ratio_as_percent():
    e = Expected.model_validate(
        {"mode": "value", "columns": {"rate": {"role": "value", "kind": "ratio"}}}
    )
    facts = [FactSpec(column="rate")]
    assert answer_facts(facts, e, ["rate"], [[0.2512]], "The rate is 25.1%.").passed
    assert answer_facts(facts, e, ["rate"], [[0.25]], "The rate is 25%.").passed
    assert not answer_facts(facts, e, ["rate"], [[0.25]], "The rate is 30%.").passed


def test_rank_reference_and_month_keys():
    e = Expected.model_validate(
        {
            "mode": "top_k",
            "k": 1,
            "columns": {
                "month": {"role": "key", "kind": "month"},
                "orders": {"role": "value", "kind": "count"},
            },
        }
    )
    rows = [["2025-10-01", 93], ["2025-05-01", 90]]
    facts = [FactSpec(column="orders", where={"month": "@rank1"})]
    good = "October 2025 had the most orders: 93."
    assert answer_facts(facts, e, ["month", "orders"], rows, good).passed
    assert not answer_facts(
        facts, e, ["month", "orders"], rows, "May 2025 had 93 orders."
    ).passed


def test_top_k_names_with_tie():
    e = Expected.model_validate(
        {
            "mode": "top_k",
            "k": 2,
            "columns": {
                "user_id": {"role": "key", "kind": "id"},
                "spend": {"role": "value"},
            },
        }
    )
    rows = [[61959, 299.95], [99772, 289.95], [76880, 289.95]]
    assert answer_facts(
        [], e, ["user_id", "spend"], rows, "Top: 61959 and 76880."
    ).passed
    assert not answer_facts(
        [], e, ["user_id", "spend"], rows, "Top: 61959 only."
    ).passed


def test_extract_figures_reads_scales():
    values = [f.value for f in extract_figures("$1.8M, 2.5 billion, 37.2K and 12.4%")]
    assert values == [1.8e6, 2.5e9, 37200.0, 12.4]
    assert contains_figure("about 1.79 million", 1_789_731.36)
    assert not contains_figure("about 2 million", 1_789_731.36)


def test_required_facts():
    required = [
        RequiredSpec.model_validate("2019"),
        RequiredSpec(any=["no data", "zero"]),
    ]
    assert required_facts(
        required, [], "No data for 2018. The data starts in 2019."
    ).passed
    result = required_facts(required, [], "Revenue in 2018 was 5.")
    assert not result.passed and result.details["missing"]
    assert not required_facts([], ["Levi"], "Your brands and Levi's").passed


REPORT = """# Q1 report
## Summary
Text.
## Findings
- Revenue was $580,426.
## Reasons
Text.
## Action items
1. Do it.
## Date range and definitions
Q1 2026.
## Method
SQL.
"""


def test_report_structure():
    assert report_structure(REPORT).passed
    result = report_structure(REPORT.replace("## Method", "## Other"))
    assert not result.passed and result.details["missing"] == ["method"]
    assert report_structure(None).reason == "section_missing"


def test_report_file(tmp_path):
    html = tmp_path / "r.html"
    html.write_text("<html></html>")
    assert report_file([str(html)], ".html").passed
    assert report_file([str(tmp_path / "r.md")], None).reason == "file_missing"
