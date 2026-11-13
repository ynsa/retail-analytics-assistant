"""The run report `report.md` (spec 23, B2.7).

Parts: header, summary table with the targets of spec 50, safety gate, results by group, failures,
latency and cost, judge agreement, failed cases, skipped cases, comparison with the last run.
Before the file is saved, a scanner removes text that looks like personal data (spec 30).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from opsfleet_agent.evals.metrics import (
    EXTERNAL_STATUS,
    TARGETS,
    Metrics,
    fmt,
    is_safety,
    target_status,
)

MAX_ROWS = 5
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(
    r"(?<!\d)(?:\+\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)"
)
_COORD = re.compile(r"-?\d{1,3}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}")

SUMMARY_KEYS = [
    ("case_pass_rate", "Case pass rate"),
    ("execution_match_rate", "SQL execution-match rate"),
    ("answer_correctness", "Answer correctness"),
    ("judge_answer_rate", "Judge pass rate, answers"),
    ("judge_report_rate", "Judge pass rate, reports"),
    ("judge_safety_rate", "Judge pass rate, safety criteria"),
    ("safety_pass_rate", "Safety pass rate (gate 100%)"),
    ("latency_p95_question_s", "p95 latency of a question (s)"),
    ("latency_p95_report_s", "p95 latency of a report (s)"),
    ("internal_errors", "Cases that end with internal_error"),
    ("calls_median_question", "Median Gemini calls for each question"),
    ("limit_exceeded", "Cases that end with limit_exceeded"),
]
COST_KEYS = [
    ("latency_median_question_s", "Median latency of a question (s)"),
    ("latency_median_report_s", "Median latency of a report (s)"),
    ("tokens_in_median", "Tokens in for each answer, median"),
    ("tokens_in_p95", "Tokens in for each answer, p95"),
    ("tokens_out_median", "Tokens out for each answer, median"),
    ("tokens_out_p95", "Tokens out for each answer, p95"),
    ("calls_p95", "Gemini calls for each answer, p95"),
    ("calls_max", "Gemini calls for each answer, maximum"),
    ("tool_calls_median", "Tool calls for each answer, median"),
    ("tool_calls_max", "Tool calls for each answer, maximum"),
    ("bq_bytes_median", "BigQuery bytes for each answer, median"),
    ("bq_bytes_total", "BigQuery bytes of the run"),
    ("self_correction_share", "Turns with a new attempt"),
    ("self_correction_success", "New attempts that end with a result"),
]


def scan_text(text: str) -> str:
    """Remove text that looks like an email, a phone number or coordinates (spec 30 fallback)."""
    try:  # the output scanner of spec 30, when it exists
        from opsfleet_agent.guard.scanner import scan_output  # type: ignore[import-not-found]

        return scan_output(text).text
    except (ImportError, AttributeError):
        pass
    for pattern in (_EMAIL, _PHONE, _COORD):
        text = pattern.sub("[removed]", text)
    return text


def _table(header: list[str], rows: list[list[Any]]) -> str:
    def cell(v: Any) -> str:
        return str(v).replace("|", "\\|").replace("\n", " ")

    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    return "\n".join(out)


def _rows_text(columns: list[str], rows: list[list[Any]]) -> str:
    shown = [dict(zip(columns, r, strict=False)) for r in rows[:MAX_ROWS]]
    more = f" (+{len(rows) - MAX_ROWS} rows)" if len(rows) > MAX_ROWS else ""
    return json.dumps(shown, default=str)[:600] + more


def build_report(
    meta: dict[str, Any],
    lines: list[dict[str, Any]],
    metrics: Metrics,
    agreement: dict[str, Any],
    skipped: dict[str, str],
    *,
    expected_rows: Callable[[str], tuple[list[str], list[list[Any]]] | None],
    agent_rows: Callable[[str], tuple[list[str], list[list[Any]]] | None],
    questions: dict[str, str],
    comparison: str | None = None,
) -> str:
    v = metrics.values
    commit = meta.get("git_commit", "?") + (
        " (work tree had changes)" if meta.get("git_dirty") else ""
    )
    out = [
        f"# Evaluation report: {meta['run_id']}",
        "",
        _table(
            ["Field", "Value"],
            [
                ["Date (UTC)", meta.get("created_at")],
                [
                    "Set",
                    f"{meta.get('set')} (set hash {str(meta.get('hashes', {}).get('set'))[:12]})",
                ],
                ["Agent model", meta.get("agent_model")],
                ["Judge model", meta.get("judge_model") or "no judge in this run"],
                ["Git commit", commit],
                ["Snapshot date", meta.get("snapshot_date")],
                [
                    "Table change times",
                    ", ".join(
                        f"{k} {t}" for k, t in meta.get("table_times", {}).items()
                    ),
                ],
                ["Run status", meta.get("status")],
                ["Approval", meta.get("approval")],
            ],
        ),
        "",
        "A model answer can differ from run to run. One run is a sample, not a proof.",
        "",
        "## Summary",
        "",
        _table(
            ["Metric", "Value", "Target (spec 50)", "Result"],
            [
                [
                    label,
                    fmt(v.get(key), key),
                    f"{TARGETS[key][0]} {fmt(TARGETS[key][1], key)}"
                    if key in TARGETS
                    else "–",
                    target_status(key, v.get(key)),
                ]
                for key, label in SUMMARY_KEYS
            ],
        ),
        "",
        (
            f"Scored cases: {v.get('cases', 0)}. Skipped cases: {len(skipped)}. "
            f"Cases with only outside errors: {v.get('external_errors', 0)} (not in the rates). "
            f"Cases that ran again after an outside error: {v.get('external_retries', 0)}."
        ),
        "",
        "## Safety gate",
        "",
    ]
    external = [x for x in lines if x["status"] == EXTERNAL_STATUS]
    if metrics.safety_by_category or any(is_safety(x) for x in external):
        failures = [
            x
            for x in lines
            if x["status"] == "scored" and not x["passed"] and is_safety(x)
        ]
        open_cases = [x for x in external if is_safety(x)]
        if failures:
            gate = f"FAIL ({len(failures)} failed cases)"
        elif open_cases:
            gate = f"not complete ({len(open_cases)} cases have only outside errors)"
        else:
            gate = "pass"
        out += [
            f"Gate: **{gate}**. The gate needs zero failures and a score for each case.",
            "",
            _table(
                ["Category", "Passed", "Cases"],
                [
                    [k, c["passed"], c["cases"]]
                    for k, c in sorted(metrics.safety_by_category.items())
                ],
            ),
            "",
        ]
        for x in failures:
            failed = [k for k, s in x["scorers"].items() if not s["pass"]]
            out.append(f"- {x['case_id']}: failed {', '.join(failed)}")
        out.append("")
    else:
        out += ["No safety cases in this run.", ""]

    out += ["## Results by capability, difficulty and role", ""]
    for group, label in (
        ("capability", "Capability"),
        ("difficulty", "Difficulty"),
        ("user", "User"),
    ):
        groups = metrics.by_group.get(group) or {}
        if not groups:
            continue
        out += [
            _table(
                [label, "Cases", "Pass rate", "Execution match", "Answer correctness"],
                [
                    [
                        k,
                        g["cases"],
                        fmt(g["case_pass_rate"], "rate"),
                        fmt(g["execution_match_rate"], "rate"),
                        fmt(g["answer_correctness"], "rate"),
                    ]
                    for k, g in groups.items()
                ],
            ),
            "",
        ]

    out += ["## Failures by error type and by scorer reason", ""]
    out += [
        _table(
            ["Error type", "Spans", "Cases that ended with it"],
            [[k, c["spans"], c["cases"]] for k, c in metrics.failures_by_error.items()]
            or [["–", 0, 0]],
        ),
        "",
        _table(
            ["Scorer reason", "Failed cases"],
            [[k, c] for k, c in sorted(metrics.failures_by_reason.items())]
            or [["–", 0]],
        ),
        "",
        "## Latency, tokens, calls and cost",
        "",
        _table(
            ["Metric", "Value"],
            [[label, fmt(v.get(key), key)] for key, label in COST_KEYS],
        ),
        "",
    ]
    plan = meta.get("plan", {})
    actual = meta.get("actual", {})
    out += [
        _table(
            ["Cost", "Planned", "Actual"],
            [
                [
                    "Gemini calls, agent",
                    plan.get("agent_calls"),
                    actual.get("agent_calls"),
                ],
                [
                    "Gemini calls, agent, in discarded runs (in the line above)",
                    "–",
                    actual.get("discarded_calls", 0),
                ],
                [
                    "Gemini calls, judge",
                    plan.get("judge_calls"),
                    actual.get("judge_calls"),
                ],
                ["Tokens in", plan.get("tokens_in"), actual.get("tokens_in")],
                ["Tokens out", plan.get("tokens_out"), actual.get("tokens_out")],
                [
                    "BigQuery bytes, agent",
                    fmt(plan.get("bq_agent_bytes"), "bytes"),
                    fmt(actual.get("bq_bytes"), "bytes"),
                ],
                [
                    "BigQuery bytes, snapshots",
                    fmt(plan.get("bq_snapshot_bytes"), "bytes"),
                    fmt(actual.get("snapshot_bytes"), "bytes"),
                ],
                [
                    "Price at the paid tier (USD)",
                    plan.get("cost_usd"),
                    actual.get("cost_usd"),
                ],
            ],
        ),
        "",
    ]
    if metrics.tool_counts:
        out += [_table(["Tool", "Calls"], sorted(metrics.tool_counts.items())), ""]

    out += ["## Judge", ""]
    if metrics.judge_criteria:
        out += [
            _table(
                ["Rubric:criterion", "Passed", "Judged", "Rate"],
                [
                    [k, c["passed"], c["judged"], fmt(c["rate"], "rate")]
                    for k, c in metrics.judge_criteria.items()
                ],
            ),
            "",
        ]
    labels = agreement.get("labels", 0)
    out += [
        (
            f"Human labels: {labels}. Agreement: {fmt(agreement.get('agreement'), 'rate')}. "
            f"True-positive rate: {fmt(agreement.get('tpr'), 'rate')}. "
            f"True-negative rate: {fmt(agreement.get('tnr'), 'rate')}."
        ),
        "",
    ]
    if labels < 20:
        out += [
            (
                "**Warning:** fewer than 20 human labels. The rates are a spot check, not a "
                "statistical proof. Add labels with `uv run opsfleet-eval review --run "
                f"{meta['run_id']}`."
            ),
            "",
        ]

    out += ["## Failed cases", ""]
    failed = [x for x in lines if x["status"] == "scored" and not x["passed"]]
    if not failed:
        out += ["No failed cases.", ""]
    for x in failed:
        reasons = {k: s["reason"] for k, s in x["scorers"].items() if not s["pass"]}
        critiques = [
            f"{c['id']}: {c['critique']}"
            for s in x["scorers"].values()
            for c in s.get("details", {}).get("criteria", [])
            if not c["passed"]
        ]
        exp = expected_rows(x["case_id"])
        got = agent_rows(x["case_id"])
        traces = [t["trace_id"] for t in x["turns"] if t.get("trace_id")]
        out += [
            f"### {x['case_id']} ({x.get('user')}, {x.get('capability')})",
            "",
            f"- Question: {questions.get(x['case_id'], '')}",
            f"- Reasons: {', '.join(f'{k}={r}' for k, r in reasons.items())}",
            f"- Expected: {_rows_text(*exp) if exp else '–'}",
            f"- Agent: {_rows_text(*got) if got else '–'}",
        ]
        if critiques:
            out.append(f"- Judge: {' / '.join(critiques)}")
        if x.get("error"):
            out.append(f"- Error: {x['error']}")
        out += [f"- Trace: {', '.join(traces) or '–'}", ""]

    out += ["## Cases with only outside errors", ""]
    out += [
        f"- {x['case_id']}: {x.get('external_error') or 'outside error'}"
        for x in external
    ] or ["None."]
    out += [""]
    out += ["## Skipped cases", ""]
    out += [f"- {k}: {r}" for k, r in sorted(skipped.items())] or ["None."]
    out += ["", "## Comparison with the last run of the same set", ""]
    out += [comparison or "No earlier run of this set."]
    return scan_text("\n".join(out) + "\n")


def write_report(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path
