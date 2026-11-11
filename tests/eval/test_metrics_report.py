"""Metrics, judge agreement, report scanner, review sample and the CLI plan (spec 23 B4)."""

from __future__ import annotations

from typer.testing import CliRunner

from opsfleet_agent.config import Secrets
from opsfleet_agent.evals import cli
from opsfleet_agent.evals.metrics import compute_metrics, judge_agreement, percentile
from opsfleet_agent.evals.report import scan_text
from opsfleet_agent.evals.review import pick_items


def line(case_id: str, passed: bool, **scorers: bool) -> dict:
    return {
        "case_id": case_id,
        "set": "accuracy",
        "kind": "accuracy",
        "user": "ceo",
        "capability": "time_metrics",
        "difficulty": "easy",
        "status": "scored",
        "passed": passed,
        "scorers": {
            k: {"pass": v, "reason": None if v else "wrong_value", "details": {}}
            for k, v in scorers.items()
        },
        "turns": [
            {
                "latency_ms": 2000,
                "llm_calls": 3,
                "tokens_in": 1000,
                "tokens_out": 100,
                "tool_calls": 0,
                "tool_names": {},
                "bq_queries": 1,
                "bq_bytes_billed": 10,
                "self_corrections": 0,
                "corrected": False,
                "error_types": [],
                "error_type": None,
                "is_report": False,
            }
        ],
    }


def test_core_rates():
    lines = [
        line("A", True, execution_match=True, answer_facts=True),
        line("B", False, execution_match=False, answer_facts=True),
        line("C", False, facts=False),
    ]
    m = compute_metrics(lines).values
    assert m["case_pass_rate"] == 1 / 3
    assert m["execution_match_rate"] == 1 / 2
    assert m["answer_correctness"] == 2 / 3
    assert m["calls_median_question"] == 3 and m["latency_p95_question_s"] == 2.0
    assert compute_metrics(lines).failures_by_reason == {"wrong_value": 2}


def test_percentile():
    assert percentile([1, 2, 3, 4, 100], 0.95) == 100
    assert percentile([], 0.5) is None


def test_judge_agreement_rates():
    items = [
        {"case_id": "A", "key": "j", "passed": True},
        {"case_id": "B", "key": "j", "passed": False},
        {"case_id": "C", "key": "j", "passed": True},
    ]
    labels = [
        {"case_id": "A", "key": "j", "human_pass": True},
        {"case_id": "B", "key": "j", "human_pass": False},
        {"case_id": "C", "key": "j", "human_pass": False},
    ]
    a = judge_agreement(labels, items)
    assert a["labels"] == 3 and a["tpr"] == 1.0 and a["tnr"] == 0.5


def test_report_scanner_removes_personal_data():
    text = scan_text("Mail jane@example.com or call 555-123-4567 at 40.7128, -74.0060.")
    assert "example.com" not in text and "555" not in text and "40.7128" not in text


def test_review_sample_alternates_fails_and_passes():
    items = [{"case_id": str(i), "key": "k", "passed": i % 3 == 0} for i in range(9)]
    picked = pick_items(items, set(), 4)
    assert [i["passed"] for i in picked] == [False, True, False, True]
    assert pick_items(items, {(i["case_id"], "k") for i in items}, 4) == []


def test_cli_plan_makes_no_call(config, monkeypatch):
    offline = config.model_copy(
        update={"secrets": Secrets(google_api_key=None, google_cloud_project=None)}
    )
    monkeypatch.setattr(cli, "load_config", lambda: offline)
    result = CliRunner().invoke(cli.app, ["plan", "--set", "smoke"])
    assert result.exit_code == 0, result.output
    assert "Gemini calls: 10 planned" in result.output
    assert "no snapshot check" in result.output  # no BigQuery in the unit test
