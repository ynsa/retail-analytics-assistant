"""Compare two runs: metric changes, cases that changed, paired counts (spec 23, B2.8; spec 51)."""

from __future__ import annotations

from typing import Any

from opsfleet_agent.evals.metrics import compute_metrics, fmt

COMPARE_KEYS = [
    "case_pass_rate",
    "execution_match_rate",
    "answer_correctness",
    "safety_pass_rate",
    "judge_answer_rate",
    "judge_report_rate",
    "judge_safety_rate",
    "latency_p95_question_s",
    "latency_p95_report_s",
    "calls_median_question",
    "tokens_in_median",
    "tokens_out_median",
    "bq_bytes_total",
    "self_correction_share",
    "internal_errors",
    "limit_exceeded",
]


def paired_counts(
    a: list[dict[str, Any]], b: list[dict[str, Any]]
) -> dict[str, list[str]]:
    pa = {x["case_id"]: x["passed"] for x in a if x["status"] == "scored"}
    pb = {x["case_id"]: x["passed"] for x in b if x["status"] == "scored"}
    common = sorted(set(pa) & set(pb))
    return {
        "only_a": [c for c in common if pa[c] and not pb[c]],
        "only_b": [c for c in common if pb[c] and not pa[c]],
        "both": [c for c in common if pa[c] and pb[c]],
        "neither": [c for c in common if not pa[c] and not pb[c]],
        "not_in_both": sorted(set(pa) ^ set(pb)),
    }


def warnings(meta_a: dict[str, Any], meta_b: dict[str, Any]) -> list[str]:
    out = []
    ha, hb = meta_a.get("hashes", {}), meta_b.get("hashes", {})
    if ha.get("set") != hb.get("set"):
        out.append("The set files differ. The runs are not comparable case by case.")
    for name in ("prompts", "rubrics", "settings"):
        if ha.get(name) != hb.get(name):
            out.append(f"The {name} differ.")
    for name in ("agent_model", "judge_model", "git_commit"):
        if meta_a.get(name) != meta_b.get(name):
            out.append(f"{name}: {meta_a.get(name)} -> {meta_b.get(name)}.")
    return out


def compare_runs(
    meta_a: dict[str, Any],
    lines_a: list[dict[str, Any]],
    meta_b: dict[str, Any],
    lines_b: list[dict[str, Any]],
    heading: bool = True,
) -> str:
    va = compute_metrics(lines_a).values
    vb = compute_metrics(lines_b).values
    rows = []
    for key in COMPARE_KEYS:
        a, b = va.get(key), vb.get(key)
        change = "–"
        if isinstance(a, int | float) and isinstance(b, int | float):
            delta = b - a
            change = fmt(delta, key) if delta else "0"
            if delta > 0 and not change.startswith("-"):
                change = "+" + change
        rows.append(f"| {key} | {fmt(a, key)} | {fmt(b, key)} | {change} |")
    pairs = paired_counts(lines_a, lines_b)
    out = []
    if heading:
        out += [f"# Comparison: {meta_a['run_id']} (A) and {meta_b['run_id']} (B)", ""]
    for w in warnings(meta_a, meta_b):
        out.append(f"- **Warning:** {w}")
    out += [
        "",
        "| Metric | A | B | Change |",
        "|---|---|---|---|",
        *rows,
        "",
        (
            f"Paired counts: only A passed {len(pairs['only_a'])} (c), only B passed "
            f"{len(pairs['only_b'])} (b), both passed {len(pairs['both'])}, both failed "
            f"{len(pairs['neither'])}."
        ),
        "",
        "Pass to fail (A passed, B failed): "
        + (", ".join(pairs["only_a"]) or "none")
        + ".",
        "",
        "Fail to pass (A failed, B passed): "
        + (", ".join(pairs["only_b"]) or "none")
        + ".",
    ]
    if pairs["not_in_both"]:
        out += ["", "Cases in only one run: " + ", ".join(pairs["not_in_both"]) + "."]
    return "\n".join(out) + "\n"
