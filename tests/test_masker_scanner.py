"""Result masker and output scanner (spec 30, B2.5, B2.6 and B4 cases 63-69)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd
import pytest

from opsfleet_agent.guard.masker import MASKED, ResultMasker
from opsfleet_agent.guard.rules import SCANNER_NOTE
from opsfleet_agent.guard.scanner import REMOVED, OutputScanner
from opsfleet_agent.tracing import init_tracing
from tests.conftest import frame_result


def read_spans(directory) -> list[dict]:
    return [
        json.loads(line)
        for f in directory.glob("*.jsonl")
        for line in f.read_text(encoding="utf-8").splitlines()
    ]


# --- masker ------------------------------------------------------------------------------------


def test_case_63_pii_column_is_dropped_with_an_internal_error_span(tmp_path):
    init_tracing(tmp_path)
    frame = pd.DataFrame(
        {"user_id": [1], "email": ["anna@example.com"], "spend": [10.0]}
    )
    masked = ResultMasker().mask(frame_result(frame))
    assert masked.columns == ["user_id", "spend"] and masked.dropped_columns == [
        "email"
    ]
    assert "anna" not in masked.data_block()
    [sp] = [s for s in read_spans(tmp_path) if s["name"] == "guard.mask"]
    assert sp["kind"] == "guard" and sp["status"] == "error"
    assert sp["attributes"]["masker_hit"] is True
    assert sp["attributes"]["error_type"] == "internal_error"
    assert "anna" not in json.dumps(sp)


def test_case_64_value_with_an_email_is_masked():
    frame = pd.DataFrame({"name": ["Shirt ann@x.com edition", "Jeans"], "n": [1, 2]})
    masked = ResultMasker().mask(frame_result(frame))
    assert masked.rows == [(MASKED, 1), ("Jeans", 2)] and masked.masked_cells == 1


def test_value_with_a_phone_number_is_masked():
    frame = pd.DataFrame({"name": ["call 555-123-4567"], "sku": ["1234567890"]})
    masked = ResultMasker().mask(frame_result(frame))
    assert masked.rows == [(MASKED, "1234567890")]


def test_case_65_long_text_is_cut_to_200_characters():
    masked = ResultMasker().mask(frame_result(pd.DataFrame({"name": ["x" * 5000]})))
    assert len(masked.rows[0][0]) == 200


def test_case_66_at_most_200_rows():
    masked = ResultMasker().mask(frame_result(pd.DataFrame({"n": range(500)})))
    assert masked.row_count == 200 and masked.truncated


def test_demographic_columns_are_dropped_from_customer_level_rows():
    frame = pd.DataFrame(
        {"user_id": [1], "state": ["Texas"], "customer_age": [30], "spend": [5]}
    )
    masked = ResultMasker().mask(frame_result(frame))
    assert masked.columns == ["user_id", "spend"]
    assert masked.dropped_columns == ["state", "customer_age"]


def test_guard_flag_marks_customer_level_rows_without_an_id_column():
    frame = pd.DataFrame({"state": ["Texas"], "spend": [5]})
    flagged = ResultMasker().mask(
        frame_result(frame), SimpleNamespace(customer_level=True)
    )
    assert flagged.columns == ["spend"]
    totals = ResultMasker().mask(
        frame_result(frame), SimpleNamespace(customer_level=False)
    )
    assert totals.columns == ["state", "spend"]


def test_data_block_holds_a_markdown_table():
    frame = pd.DataFrame({"brand": ["Levi's", "A|B"], "revenue": [1.5, None]})
    block = ResultMasker().mask(frame_result(frame)).data_block("r2")
    assert block.startswith('<data source="query" ref="r2">') and block.endswith(
        "</data>"
    )
    assert "| brand | revenue |" in block and "| A\\|B |  |" in block


# --- scanner -----------------------------------------------------------------------------------


def test_case_67_each_pattern_is_removed_and_the_note_is_added(tmp_path):
    init_tracing(tmp_path)
    text = (
        "Write to anna.smith@example.com or call +1 (555) 123-4567 or 555-123-4567. "
        "She lives at 12 Oak Street and works at 400 W 5th Ave. Location: 40.7128, -74.0060."
    )
    result = OutputScanner().scan(text)
    assert result.hits == {
        "email": 1,
        "phone": 2,
        "street_address": 2,
        "coordinates": 1,
    }
    for secret in ("anna", "555", "Oak", "5th", "40.7128"):
        assert secret not in result.text
    assert result.text.count(REMOVED) == 6
    assert result.with_note().endswith(f"_{SCANNER_NOTE}_")
    [sp] = [s for s in read_spans(tmp_path) if s["name"] == "guard.scan"]
    assert sp["attributes"]["hits_total"] == 6 and sp["attributes"]["hits_phone"] == 2
    assert "anna" not in json.dumps(sp) and "Oak" not in json.dumps(sp)


@pytest.mark.parametrize(
    "text",
    [
        "Revenue was 1,234,567.89 in 2024-03",
        "order 1234567890",
        "Period: 2024-01-01 to 2024-03-31. 6,290 orders, $385,083.24 revenue.",
        "Average spend 12.34 56.78 and a rate of 12.4%.",
        "Calvin Klein sold 12 items in the Way to Go collection.",
        "| 2024-10 | 1,204 | $55,310.12 |",
        "Customer 4521 spent $1,200.50 on 3 orders.",
        "Coordinates 1.5, 2.5 are not precise.",
    ],
)
def test_case_68_figures_are_not_removed(text):
    result = OutputScanner().scan(text)
    assert result.hits == {} and result.text == text
    assert result.with_note() == text


def test_case_69_html_report_cell_with_an_email_is_cleaned():
    html = "<table><tr><td>4521</td><td>anna@example.com</td></tr></table>"
    result = OutputScanner().scan(html, target="report_html")
    assert result.text == f"<table><tr><td>4521</td><td>{REMOVED}</td></tr></table>"
    assert result.hits == {"email": 1}


def test_coordinates_outside_the_ranges_stay():
    assert OutputScanner().scan("Ratio 123.4567, 200.1234").hits == {}
